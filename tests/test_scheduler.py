"""Unit tests for emrg.server.scheduler — task loading, migration, and lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server.scheduler import (
    TaskHandler,
    TaskScheduler,
    _resolve_project_path,
)
from tests.task_handler_factory import make_handler


# ── _resolve_project_path ─────────────────────────────────────────


def test_task_handler_logger_adapter_carries_task_column(tmp_path):
    """Rant 2026-08-19T10:18:44: TaskHandler logs carry a dedicated `task`
    extra (LoggerAdapter) so the daemon's Formatter can render a [task]
    column — scheduler lines are otherwise indistinguishable in emrgd.log."""
    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = make_handler(
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


# ── write_table / read_table: the only writer and the only reader ──


def test_write_table_replaces_atomically(tmp_path):
    """write_table swaps the whole table; it never appends."""
    from emrg.server.scheduler import write_table

    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([{"name": "old"}]))

    write_table([
        {"name": "test1", "type": "evolution", "config": {"project": "emrg"}},
    ], tasks_yml)

    data = yaml.safe_load(tasks_yml.read_text())
    assert len(data) == 1
    assert data[0]["name"] == "test1"


def test_write_table_creates_parent_dir(tmp_path):
    """write_table creates the parent directory if missing."""
    from emrg.server.scheduler import write_table

    tasks_yml = tmp_path / "deep" / "nested" / "tasks.yml"
    write_table([{"name": "deep"}], tasks_yml)

    assert yaml.safe_load(tasks_yml.read_text())[0]["name"] == "deep"


def test_a_write_keeps_multiline_strings_as_literal_blocks(tmp_path):
    """A write preserves the host's own `|` formatting (design acceptance I9).

    `yaml.safe_dump` folds a multi-line string into single-quoted, hard-wrapped
    lines, so one GUI edit used to reflow every `extra_prompt` in the file —
    including the records the edit never touched.
    """
    from emrg.server.scheduler import read_table, write_table

    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(
        "- name: a\n"
        "  type: evolution\n"
        "  config:\n"
        "    project: emrg\n"
        "  extra_prompt: |\n"
        "    first line\n"
        "    second line\n"
    )

    write_table(read_table(tasks_yml), tasks_yml)

    text = tasks_yml.read_text()
    assert "extra_prompt: |" in text, text
    assert "first line\n    second line" in text, text


def test_read_table_seeds_the_default_when_the_file_is_missing(tmp_path):
    """A missing file is seeded with the default table, not reported as empty.

    This is a general rule about the file, not an install step (host principle):
    deleting tasks.yml re-seeds `emrg-task`.
    """
    from emrg.server.scheduler import DEFAULT_TASK_RECORD, read_table

    tasks_yml = tmp_path / "tasks.yml"
    table = read_table(tasks_yml)

    assert [r["name"] for r in table] == ["emrg-task"]
    assert table[0] == DEFAULT_TASK_RECORD
    assert yaml.safe_load(tasks_yml.read_text())[0]["name"] == "emrg-task"


def test_read_table_refuses_an_unparsable_file(tmp_path):
    """Unreadable is an error, never `[]`.

    The two readings mean opposite things and only one of them is safe: "no tasks"
    retires every handler at its next wake, while "unreadable" must change nothing.
    """
    from emrg.server.scheduler import TableUnreadable, read_table

    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text("name: [unclosed\n")

    with pytest.raises(TableUnreadable):
        read_table(tasks_yml)


def test_read_table_refuses_a_root_that_is_not_a_list(tmp_path):
    """A mapping root is not a task table — same refusal, not an empty list."""
    from emrg.server.scheduler import TableUnreadable, read_table

    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text("name: emrg-task\n")

    with pytest.raises(TableUnreadable):
        read_table(tasks_yml)


# ── Task CRUD writes the file once ────────────────────────────────


def _task_create_fixture(tmp_path, monkeypatch, sched):
    """A registered project + a tasks.yml, so task_create has something to be valid against."""
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([{"name": "proj", "path": str(tmp_path / "proj")}])
    )
    sched._tasks_file = tmp_path / "tasks.yml"
    sched._tasks_file.write_text(yaml.safe_dump([
        {"name": "emrg-task", "type": "evolution", "config": {"project": "emrg"}},
    ]))


def test_task_create_writes_the_file_exactly_once(tmp_path, monkeypatch):
    """One GUI CRUD writes tasks.yml once (design acceptance I6).

    The old path saved in `task_create` and then saved again in `apply_tasks`, whose
    first line wrote the table it had just been handed.
    """
    from emrg.server import scheduler as mod

    sched = TaskScheduler(InstanceIdentity())
    _task_create_fixture(tmp_path, monkeypatch, sched)
    writes: list = []
    real = mod.write_table
    monkeypatch.setattr(
        mod, "write_table",
        lambda records, path=None: (writes.append(list(records)), real(records, path))[1],
    )

    ok, task = sched.task_create("proj-task", "evolution", "proj", interval=900)

    assert ok, task
    assert len(writes) == 1, f"expected one write, got {len(writes)}"
    data = yaml.safe_load(sched._tasks_file.read_text())
    assert [t["name"] for t in data] == ["emrg-task", "proj-task"]


def test_task_delete_and_update_do_not_touch_other_records(tmp_path, monkeypatch):
    """Each CRUD writes once and leaves every other record byte-identical."""
    from emrg.server import scheduler as mod

    sched = TaskScheduler(InstanceIdentity())
    _task_create_fixture(tmp_path, monkeypatch, sched)
    sched._tasks_file.write_text(yaml.safe_dump([
        {"name": "emrg-task", "type": "evolution", "config": {"project": "emrg"}},
        {"name": "keep", "type": "evolution", "config": {"project": "proj"}, "interval": 300},
        {"name": "drop", "type": "evolution", "config": {"project": "proj"}, "interval": 400},
    ]))
    before = yaml.safe_load(sched._tasks_file.read_text())

    assert sched.task_delete("drop") == (True, "")
    after_delete = yaml.safe_load(sched._tasks_file.read_text())
    assert [t["name"] for t in after_delete] == ["emrg-task", "keep"]
    assert after_delete[1] == before[1]

    ok, _ = sched.task_update("keep", interval=555)
    assert ok
    after_update = yaml.safe_load(sched._tasks_file.read_text())
    assert [t["name"] for t in after_update] == ["emrg-task", "keep"]
    assert after_update[1]["interval"] == 555
    assert after_update[0] == before[0]


# ── TaskScheduler.load_and_start ──────────────────────────────────


async def _boot(tmp_path, tasks=None):
    """`load_and_start` in a temp config dir; returns a snapshot of what it started.

    The snapshot is taken *inside* the loop on purpose: a handler removes itself when
    its coroutine finishes (the done-callback `_forget`), and `asyncio.run` cancels
    every pending task on its way out — so reading the live set after the call returns
    reads an empty set, which is correct and tells the test nothing.
    """
    from emrg.server import scheduler as mod

    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        if tasks is not None:
            sched._tasks_file.write_text(yaml.safe_dump(tasks))
        coros = sched.load_and_start()
        # A handler derives its record at its first wake, in a worker thread — so
        # "what template did this type resolve to" is only answerable once that read
        # has landed. Bounded wait, and the loop is the scheduler's, not a sleep.
        for _ in range(200):
            if all(h._derived for h in sched._live.values()):
                break
            await asyncio.sleep(0.01)
        snapshot = {
            "coros": len(coros),
            "names": sorted(sched._live),
            "intervals": {h.name: h.interval for h in sched._live.values()},
            "templates": {h.name: h._template_path.name for h in sched._live.values()},
        }
        sched.stop_all()
        return snapshot
    finally:
        mod.config_dir = orig_config


def test_load_and_start_no_file_seeds_the_default_table(tmp_path):
    """A missing tasks.yml is seeded from `read_table` and the seeded task starts.

    The seed is a property of the file (host principle: a file that is missing is
    written, whatever the install story is), which is why this no longer needs a
    self-heal step to run first — and why deleting the file brings `emrg-task` back.
    """
    snapshot = asyncio.run(_boot(tmp_path))

    assert snapshot["coros"] == 2, "the handler plus the reconcile loop"
    assert snapshot["names"] == ["emrg-task"]
    assert snapshot["intervals"] == {"emrg-task": 600}, "the seeded default, not 60"


def test_load_and_start_enabled_task(tmp_path):
    """Starts a coroutine for each enabled task, and the interval is the record's."""
    snapshot = asyncio.run(_boot(tmp_path, [
        {"name": "emrg", "type": "evolution", "config": {"project": "emrg"},
         "interval": 99, "enabled": True},
    ]))

    assert snapshot["coros"] == 2  # the handler plus the reconcile loop
    assert snapshot["names"] == ["emrg"], (
        "the file exists, so nothing is seeded on top of it (D2)"
    )
    assert snapshot["intervals"] == {"emrg": 99}


def test_load_and_start_skips_disabled(tmp_path):
    """Disabled tasks are not started."""
    snapshot = asyncio.run(_boot(tmp_path, [
        {"name": "enabled", "type": "evolution", "config": {"project": "emrg"}, "enabled": True},
        {"name": "disabled", "type": "evolution", "config": {"path": "/tmp"}, "enabled": False},
    ]))

    assert snapshot["coros"] == 2  # the handler plus the reconcile loop
    assert snapshot["names"] == ["enabled"]


def test_load_and_start_skips_an_unknown_type_and_says_so(tmp_path, caplog):
    """An unknown type is logged and skipped (design acceptance I5).

    The old loader fell back to `evolution_prompt.md` in silence: a typo'd `type` in
    a hand-edited file ran the evolution prompt for a task that meant something else,
    and nothing said so. The IPC-side validator always rejected it — so the two paths
    disagreed, and the hand-edited file was the one that took the silent route.
    """
    with caplog.at_level(logging.ERROR):
        snapshot = asyncio.run(_boot(tmp_path, [
            {"name": "bad", "type": "nonexistent_handler", "config": {}, "enabled": True},
        ]))

    assert snapshot["names"] == [], "an unknown type is not started"
    assert "unknown type" in caplog.text


def test_task_templates_and_the_default_type(tmp_path):
    """Every built-in type names a template file that exists.

    Regression guard: template path bugs (promote #304, #306) crashed the scheduler at
    runtime, and this test fails fast if a mapping loses its template or a template
    file is renamed. The second half is new with the `HANDLERS` census removal: the
    type the default task record names must resolve, or a fresh install starts nothing.
    """
    from emrg.server import scheduler as mod

    assert mod.DEFAULT_TASK_RECORD["type"] in mod.TASK_TEMPLATES
    for task_type, filename in mod.TASK_TEMPLATES.items():
        template_path = Path(__file__).resolve().parent.parent / "emrg" / "server" / filename
        assert template_path.exists(), (
            f"template file missing for {task_type!r}: {template_path.name}"
        )


# ── Template task types (paper / open-source / promote) ────────────


def test_load_and_start_competition_task(tmp_path):
    """Competition tasks start a TaskHandler with the competition template.

    Rant 2026-09-12T14:57:33: `competition` is a built-in task type. It reuses the
    shared TaskHandler — only the template differs — so the failure mode this guards is
    the wiring: a type whose template is missing or misnamed crashes at start.
    """
    snapshot = asyncio.run(_boot(tmp_path, [
        {"name": "competition-task", "type": "competition",
         "config": {"project": "competitions"},
         "interval": 3600, "enabled": True},
    ]))

    assert snapshot["coros"] == 2  # the handler plus the reconcile loop
    assert snapshot["templates"] == {"competition-task": "competition_prompt.md"}


def test_load_and_start_promote_task(tmp_path):
    """Promote tasks start a TaskHandler with the promote template."""
    snapshot = asyncio.run(_boot(tmp_path, [
        {"name": "olr-promote", "type": "promote",
         "config": {"project": "openlocalrouter"},
         "interval": 3600, "enabled": True},
    ]))

    assert snapshot["templates"] == {"olr-promote": "promote_prompt.md"}


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


def test_resolve_task_template_journal_builtin_priority(tmp_path):
    """journal is built-in: user task-templates/journal.md must NOT shadow it.

    rant 2026-08-24T09:50:13: journal promoted from custom template to builtin;
    built-in template takes priority over the user template.
    """
    from emrg.server import scheduler as mod
    (tmp_path / "task-templates").mkdir()
    (tmp_path / "task-templates" / "journal.md").write_text(
        "# evil override\n", encoding="utf-8"
    )
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        resolved = mod._resolve_task_template("journal")
        assert resolved.name == "journal_prompt.md"
        assert "task-templates" not in str(resolved)
        assert resolved.exists()
    finally:
        mod.config_dir = orig_config


def test_load_and_start_journal_task(tmp_path):
    """Journal tasks start a TaskHandler with the journal template."""
    snapshot = asyncio.run(_boot(tmp_path, [
        {"name": "journal-task", "type": "journal",
         "config": {"project": "mem", "role": "author", "author_id": "author-a"},
         "interval": 3600, "enabled": True},
    ]))

    assert snapshot["templates"] == {"journal-task": "journal_prompt.md"}


def test_task_create_journal_without_custom_template(tmp_path):
    """type=journal creates fine even when no user template exists.

    rant 2026-08-24T09:50:13 acceptance: after deleting
    ~/.emrg/task-templates/journal.md, a type=journal task must still be
    creatable (builtin registration covers validation + resolution).
    """
    mod, orig = _p2_env(tmp_path)  # tmp_path has NO task-templates/ dir
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        ok, task = sched.task_create("journal-task", "journal", "emrg", 300)
        assert ok, task
        assert task["type"] == "journal"
        assert mod._resolve_task_template("journal").name == "journal_prompt.md"
        # non-builtin type with no custom template is still rejected
        ok, err = sched.task_create("other", "no-such-type", "emrg", 60)
        assert not ok and "unknown task type" in err
    finally:
        mod.config_dir = orig


def test_journal_template_renders_with_context():
    """journal_prompt.md renders without Jinja2 errors and covers the 4 follow-up rants.

    rants 2026-08-24T18:09:13/18:15:31/18:16:50/18:19:08 — contribution-level
    assessment, de-EMRG-ified scope, direction diversity, research-hotspot focus
    + current-time injection must all be present in the rendered output for both
    author and editor roles.
    """
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "journal_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    ctx = dict(
        instance_id="test", host_name="host", owner="x", repo="y", source_dir="/tmp/j", timestamp="20260824-182903",
        current_time_human="2026-08-24 18:29",
        task={"role": "author", "project": "j", "author_id": "author-a"},
    )
    out = template.render(**ctx)
    # Current-time injection (rant 18:19:08)
    assert "Current time:" in out and "20260824-182903" in out
    assert "2026-08-24 18:29" in out, "human-readable time must render"
    # De-EMRG-ified scope (rant 18:15:31) — external scan first, no concrete examples
    assert "External scan (MUST do, first priority)" in out
    assert "NOT from any specific project or system name" in out
    assert "self-evolving agents" not in out, "EMRG-specific arXiv example removed"
    assert "agent self-improvement" not in out, "EMRG-specific arXiv example removed"
    # Adversarial checks (rant 18:09:13)
    assert "Adversarial checks" in out
    assert "Reverse gap check" in out
    assert "Evidence pre-assessment" in out
    assert "Upgradability" in out
    # Direction diversity (rant 18:16:50)
    assert "direction-diversity check" in out
    assert "host specified → obey host" in out
    # Contribution-level declaration (rant 18:09:13)
    assert "Contribution-level self-declaration" in out
    assert "case study" in out and "theory+empirics" in out
    # Irrelevant-rant exclusion (rant 18:15:31)
    assert "Irrelevant-rant exclusion" in out
    # Editor role renders too (diversity/adversarial text is author-side)
    ctx2 = dict(ctx); ctx2["task"] = {"role": "editor", "project": "j"}
    out2 = template.render(**ctx2)
    assert "Editor Work Cycle" in out2
    assert "Contribution-level consistency" in out2 or "contribution-level" in out2


# ── TaskHandler core ─────────────────────────────────────────


def test_evolution_handler_project_path_fallback():
    """Without config.project or config.path, name is the fallback path."""
    handler = make_handler(
        name="emrg",
        config={},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler.project_path == "emrg"


def test_evolution_handler_project_path_from_config():
    """config.path is used when config.project is empty."""
    handler = make_handler(
        name="emrg",
        config={"path": "/custom/path"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler.project_path == "/custom/path"


def test_evolution_handler_stop():
    """stop() sets _running to False."""
    handler = make_handler(
        name="test", config={}, interval=60,
        identity=InstanceIdentity(),
    )
    handler._running = True
    handler.stop()
    assert handler._running is False


def test_evolution_handler_status_last_run_fields():
    """status() exposes last-run + saturation (rant 2026-08-18T10:45:52)."""
    from emrg.protocol import EvolutionLog
    handler = make_handler(
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
    # rant 2026-08-21T17:46:12: task→session link fields (GUI open-session action)
    assert st["session_id"] == "emrg-evolution-test"
    assert st["project"] == ""  # config={} → empty project name
    assert st["project_path"] == "test"  # fallback path = name
    # rant 2026-08-22T07:18:35: started_at = per-CYCLE start epoch (set at tick
    # begin, cleared at cycle end), only valid while running; None when idle
    assert st["started_at"] is None, "idle handler → started_at None"
    handler._cycle_running = True
    handler._cycle_start_time = 1_700_000_000.0
    assert handler.status()["started_at"] == 1_700_000_000.0, "running → cycle start epoch"
    handler._cycle_running = False
    assert handler.status()["started_at"] is None, "completed → started_at None again"
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
    handler = make_handler(
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
        h1 = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
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
        h2 = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
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


def test_task_handler_task_runs_are_never_truncated(tmp_path):
    """Appending never rewrites the file, and a restart restores every record.

    The reverse of what this test asserted until rant 2026-09-29T09:29:21: a
    `_TASK_RUNS_MAX = 50` cap trimmed both the file and the restored list, which
    made the restored list a window rather than a history — and, because the
    daemon printed `len(self.evolutions)` as `{{ evolution_count }}`, a window
    that was read as a total. Mutation: putting the trim block back must fail
    the `== 60` / `run-0` assertions below.
    """
    from emrg.protocol import EvolutionLog
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        h1 = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        for i in range(60):
            h1.evolutions.append(EvolutionLog(
                timestamp=f"2026-08-19T20:{i % 60:02d}:00",
                work=f"run-{i}",
                tool_count=i,
            ))
            h1._append_task_run(h1.evolutions[-1])

        f = tmp_path / "logs" / "task-runs" / "emrg-task.jsonl"
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 60, "every appended record is still in the file"
        # a fresh handler restores all 60, oldest first
        h2 = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        assert len(h2.evolutions) == 60
        assert h2.evolutions[-1].work == "run-59"
        assert h2.evolutions[0].work == "run-0"
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
        handler = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
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
        handler = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
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
    handler = make_handler(
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
    handler = make_handler(
        name="emrg-task",
        config={"project": "emrg"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler._repo_configured is True
    assert handler._repo == "emrg"


def test_task_handler_repo_configured_from_config():
    """config owner/repo overrides the default and enables self-heal (rant 18:14:46 P1)."""
    handler = make_handler(
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
    handler = make_handler(
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
    h1 = make_handler(name="a", config={}, interval=60, identity=InstanceIdentity())
    h2 = make_handler(name="b", config={}, interval=60, identity=InstanceIdentity())
    sched._live = {"a": h1, "b": h2}

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
        instance_id="test", host_name="host", source_dir="/tmp/paper", session_id="s1", timestamp="20260806",
        task={}, project={}, )
    # The per-round `paper_state.md` was retired (rant 2026-09-14T14:35:47): the
    # session is the state now, so the template must render the continuity contract
    # and the closing summary that replaces the file — and must not name the file.
    assert "Cross-round continuity" in out, "跨轮续接指引应渲染"
    assert "closing summary in your final message" in out, "收尾总结契约应渲染"
    assert "paper_state.md" not in out, "已废置的状态文件指引不应再渲染"
    assert "latexmk" in out, "LaTeX 检查指引应渲染"
    assert "literature" in out, "文献去重指引应渲染"


def test_journal_template_renders_the_continuity_contract():
    """journal_prompt.md renders, and its continuity contract replaces the retired files."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "journal_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", source_dir="/tmp/journal", session_id="s1", timestamp="20260919-100129",
        current_time_human="2026-09-19 10:01", task={"role": "editor", "project": "silicon-science-cs"},
        project={}, owner="argszero", repo="silicon-science-cs",
    )
    # The state and reflection files were retired (rant 2026-09-14T14:35:47): the
    # session is the state now, so the template must render the continuity contract
    # and the closing summary that replaced them — and must not name either file.
    assert "Cross-round continuity" in out, "跨轮续接指引应渲染"
    assert "closing summary in your final message" in out, "收尾总结契约应渲染"
    assert "_state.md" not in out, "已废置的状态文件路径不应再渲染"
    assert "_reflections.md" not in out, "已废置的反思文件路径不应再渲染"
    # The role-gated work cycles still render, and the session is named as the carrier.
    assert "Editor Work Cycle" in out, "编辑器工作周期应渲染"
    assert "s1" in out, "会话 id（状态载体）应渲染"


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
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        timestamp="20260813",
        task={"role": "committer", "project": "aitokenpool"},
        project={}, git_path="git", gh_path="gh",
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
    # Rant 2026-08-18T16:42:52: `submit_rant` is the only writer of rants.jsonl.
    # This assertion used to require the opposite — that the rendered section state
    # the field order / sort / `json.dumps(..., ensure_ascii=False)` rule — which
    # made the *tests* pin the restated rule the templates were then swept of
    # (cyc20260914-201054). A rendering test may assert that the section survives
    # Jinja with this context; it may not assert that a rule the tool owns is
    # restated here, because the whole point of the sweep is that it is not.
    assert 'submit_rant(action="update"' in out, "rant 状态写入应改为交给 submit_rant"
    assert "json.dumps(..., ensure_ascii=False)" not in out, (
        "the field order / sort / encoding rule is `submit_rant`'s; the rendered "
        "section must not restate it (see tests/test_rants_single_writer.py)"
    )


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
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        timestamp="20260814",
        project={}, git_path="git", gh_path="gh",
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


def test_task_extra_prompt_conditional_all_templates():
    """Rant 2026-08-24T21:59:15: tasks.yml config extra_prompt must inject into
    every builtin task prompt (evolution/paper/open-source/promote/journal) near
    the top; tasks WITHOUT extra_prompt render unchanged (no section, no residue).

    Positive AND negative states both validated (#455 lesson).
    """
    import jinja2

    env = jinja2.Environment(undefined=jinja2.Undefined)
    server_dir = Path(__file__).resolve().parent.parent / "emrg" / "server"
    base = dict(
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        timestamp="20260824",
        current_time_human="2026-08-24 22:00",
        project={"name": "x"}, git_path="git", gh_path="gh",
    )
    extra = "插件开发（plugin）也是对本项目的合法贡献，不应被排除在贡献范围之外"
    heading = "Task-specific Instructions (extra_prompt from tasks.yml)"
    for name in ("evolution_prompt.md", "paper_prompt.md", "open_source_prompt.md",
                 "promote_prompt.md", "journal_prompt.md"):
        tpl = env.from_string((server_dir / name).read_text(encoding="utf-8"))
        # positive: extra_prompt configured → injected near the top
        out = tpl.render(task={"extra_prompt": extra, "role": "committer"}, **base)
        assert heading in out, f"{name}: 条件节应渲染"
        assert extra in out, f"{name}: extra_prompt 文本应注入 prompt"
        # negative: absent → no section, no residue
        out2 = tpl.render(task={"role": "committer"}, **base)
        assert heading not in out2, f"{name}: 未配置 extra_prompt 不得出现条件节"
        assert extra not in out2, f"{name}: 未配置 extra_prompt 不得注入文本"


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
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        timestamp="20260820",
        task={"role": "committer", "project": "aitokenpool"},
        project={}, git_path="git", gh_path="gh",
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
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        timestamp="20260814",
        task={"role": "committer", "project": "aitokenpool"},
        project={}, git_path="git", gh_path="gh",
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


def test_open_source_template_parallel_recon_c15():
    """open_source_prompt.md Phase C.1.5 allows parallel Recon when all open
    PRs are healthy (rant 2026-08-24T14:05:06): a long-lived healthy PR must
    not lock the task out of producing new contributions."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "open_source_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/os", source_dir="/tmp/os", session_id="s1",
        timestamp="20260824",
        task={"role": "committer", "project": "aitokenpool"},
        project={}, git_path="git", gh_path="gh",
    )
    # 1) the new section exists
    assert "C.1.5 Parallel Recon" in out, "C.1.5 并行 Recon 节应渲染"
    assert "not maintenance-only" in out, "Tracking 非纯维护说明应渲染"
    # 2) healthy = MERGEABLE + CI green + no unaddressed feedback + no rebase due
    assert "MERGEABLE" in out, "健康判定含 MERGEABLE"
    assert "CI green" in out, "健康判定含 CI 绿"
    assert "No unaddressed feedback" in out, "健康判定含无未处理评审反馈"
    assert "No rebase maintenance due" in out, "健康判定含无需 rebase 维护"
    # 3) parallel output cap + direction diversity rules render
    assert "3 total same-day open PRs" in out, "同日 open PR 上限 3 个应渲染"
    assert "Direction diversity" in out, "方向多样性规则应渲染"
    # 4) state file stage value Track+Recon is documented in the format spec
    assert "Track+Recon" in out, "Track+Recon stage 值应渲染"
    # 5) the state machine no longer hard-locks open PRs into Tracking-only
    assert "May run Recon in parallel this round" in out, "状态机应允许并行 Recon"


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
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/pm", source_dir="/tmp/pm", session_id="s1",
        timestamp="20260814",
        task={"project": "aitokenpool"},
        project={"path": "/tmp/proj", "name": "aitokenpool", "description": "d"},
        git_path="git", gh_path="gh",
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
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/pm", source_dir="/tmp/pm", session_id="s1",
        timestamp="20260815",
        task={"project": "aitokenpool"},
        project={"path": "/tmp/proj", "name": "aitokenpool", "description": "d"},
        git_path="git", gh_path="gh",
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


def test_promote_template_direct_cdp_rule():
    """promote_prompt.md §0.3 mandates direct CDP 127.0.0.1:57000 for all
    browser ops, forbidding the remote-debugging-setup popup flow
    (rant 2026-08-25T17:57:15, R49: popup blocked waiting for host Allow)."""
    import jinja2

    template_path = (
        Path(__file__).resolve().parent.parent
        / "emrg" / "server" / "promote_prompt.md"
    )
    env = jinja2.Environment(undefined=jinja2.Undefined)
    template = env.from_string(template_path.read_text(encoding="utf-8"))
    out = template.render(
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/pm", source_dir="/tmp/pm", session_id="s1",
        timestamp="20260825",
        task={"project": "aitokenpool"},
        project={"path": "/tmp/proj", "name": "aitokenpool", "description": "d"},
        git_path="git", gh_path="gh",
    )
    # A. direct CDP endpoint mandated (positive discrimination)
    assert "127.0.0.1:57000" in out, "直连 CDP 端点应渲染"
    assert "Direct CDP connection (MUST" in out, "直连 CDP MUST 规则应渲染"
    assert "remote-debugging-setup" in out, "禁止的 remote-debugging-setup 应点名"
    assert "FORBIDDEN" in out, "禁止词应渲染"
    # B. retry-direct-not-popup behavior
    assert "retry the direct connection" in out, "直连失败应重试直连而非弹窗"


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
        instance_id="test", host_name="host", repo_url="https://github.com/x/y.git", owner="x", repo="y",
        local_source="/tmp/pm", source_dir="/tmp/pm", session_id="s1",
        timestamp="20260815",
        task={"project": "aitokenpool"},
        project={"path": "/tmp/proj", "name": "aitokenpool", "description": "d"},
        git_path="git", gh_path="gh",
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
    """Build a TaskHandler whose record points at a tmp config dir."""
    from emrg.server import scheduler as mod

    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = make_handler(
            name=name,
            config={"project": project} if project else {},
            interval=60,
        )
    finally:
        mod.config_dir = orig_config
    if path is not None:
        # What the handler reads is the derived set, so a test that needs a particular
        # resolution states it there rather than building a projects.yml to derive it.
        handler._derived["source_dir"] = str(path)
        handler._derived["project_path"] = str(path)
    return handler



def test_a_record_deleted_from_an_existing_file_stays_deleted(tmp_path):
    """D2: deleting one record is terminal — the file is not re-seeded for it.

    The seed belongs to a *missing file*, not to a missing record. Re-adding a record
    the host deleted would resurrect exactly what they removed, and the host's own
    principle for this area is that the file is the truth.
    """
    from emrg.server import scheduler as mod

    tasks_yml = tmp_path / "tasks.yml"
    tasks_yml.write_text(yaml.safe_dump([
        {"name": "other", "type": "evolution", "config": {"project": "other"}, "enabled": True},
    ]))

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tasks_yml

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_emrg_project_entry()
        sched._ensure_emrg_project_entry()  # idempotent
    finally:
        mod.config_dir = orig_config

    names = [e["name"] for e in yaml.safe_load(tasks_yml.read_text(encoding="utf-8"))]
    assert names == ["other"], "the file exists, so nothing is seeded into it"


def test_the_seed_returns_when_the_whole_file_is_deleted(tmp_path):
    """D2's other half: deleting the file brings `emrg-task` back."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import read_table

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        names = [r["name"] for r in read_table(sched._tasks_file)]
    finally:
        mod.config_dir = orig_config

    assert names == ["emrg-task"]


def test_ensure_emrg_project_entry_adds_project_entry_when_missing(tmp_path):
    """Missing projects.yml emrg entry gets added (fixed path, no network)."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import TaskScheduler

    sched = TaskScheduler(InstanceIdentity())

    orig_config = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        sched._ensure_emrg_project_entry()
        sched._ensure_emrg_project_entry()  # idempotent
    finally:
        mod.config_dir = orig_config

    projects_yml = tmp_path / "projects.yml"
    assert projects_yml.exists()
    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    emrg = next(e for e in data if e.get("name") == "emrg")
    assert emrg["path"] == str(Path.home() / ".emrg" / "evolution" / "emrg")
    assert len([e for e in data if e.get("name") == "emrg"]) == 1  # no dup


def test_ensure_emrg_project_entry_preserves_existing_project_entry(tmp_path):
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
        sched._ensure_emrg_project_entry()
    finally:
        mod.config_dir = orig_config

    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["name"] == "emrg"
    assert data[0]["path"] == str(dev_path)  # untouched


def test_ensure_emrg_project_entry_repairs_stale_project_entry(tmp_path):
    """A dead emrg path (deleted pytest-temp dir) is repaired to the canonical
    workspace (2026-08-12 incident: a test run leaked a pytest temp path into
    the real ~/.emrg/projects.yml; the dir is gone after the suite, leaving a
    dangling entry that list_projects/GUI pickers would show forever)."""
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import TaskScheduler

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
        sched._ensure_emrg_project_entry()
    finally:
        mod.config_dir = orig_config

    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    by_name = {e["name"]: e for e in data}
    assert by_name["emrg"]["path"] == (
        str(Path.home() / ".emrg" / "evolution" / "emrg")
    )  # repaired
    assert by_name["other"]["path"] == str(tmp_path / "other")  # untouched
    assert len(data) == 2


def test_ensure_emrg_project_entry_other_entries_preserved(tmp_path):
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
        sched._ensure_emrg_project_entry()
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
    """Build a fully-scripted handler for _run_evolution_cycle tests.

    When the scripted frames run out, `recv` closes the connection the way the
    daemon does. It used to raise a bare `ConnectionClosed()`, which is not a
    raiseable instance of that class in websockets 17 (`__init__` needs its
    close frames), so the TypeError sailed past the loop's
    `except ConnectionClosed` and landed in its generic `except Exception` —
    a test double exercising a path the production code never takes.
    """
    import json as _json

    from websockets.exceptions import ConnectionClosed as _Closed
    from websockets.frames import Close as _Close

    from emrg.server import scheduler as mod

    class _FakeWS:
        def __init__(self, frm):
            self._frames = list(frm)
            self.sent = []

        async def send(self, msg):
            self.sent.append(msg)

        async def recv(self):
            if self._frames:
                frame = _as_this_cycles_frame(self._frames.pop(0), self.sent)
                return _json.dumps(frame, ensure_ascii=False)
            raise _Closed(_Close(1000, ""), None)

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
        {"request_id": "self", "content": "Exceeded maximum tool call rounds (270).",
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
        {"request_id": "self", "content": "Done", "done": True,
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
        {"request_id": "self", "content": "Analyzed the issue and wrote memory",
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
        {"request_id": "self", "content": "Reviewed PR and posted LGTM",
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
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])
    asyncio.run(handler2._run_evolution_cycle())
    log2 = captured2["log"]
    assert log2.work == "", "vibe unavailable → work stays empty (no fallback)"
    assert log2.recommend_slowdown is False
    assert log2.tool_count == 0


def test_evolution_cycle_log_work_not_truncated(tmp_path):
    """Rant 2026-08-20T22:45:33: work is saved in FULL — the [:500] truncation
    is removed (it produced exactly-500-char garbage in task-run JSONL).
    A long work value round-trips through the cycle log and the JSONL intact."""
    long_work = ("完成。" + "详细产出说明。" * 120)  # ~600 chars > old 500 cap
    assert len(long_work) > 500
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": long_work, "recommend_slowdown": False,
                    "slowdown_reason": ""}},
    ])
    asyncio.run(handler._run_evolution_cycle())
    log = captured["log"]
    assert log.work == long_work, \
        "work must be persisted without [:500] truncation"
    # JSONL copy keeps the full value too
    from emrg.server import scheduler as mod
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        h = make_handler(name="emrg-task", config={}, interval=60,
                        identity=InstanceIdentity())
        assert h.evolutions, "restored from the JSONL written by the cycle"
        assert h.evolutions[-1].work == long_work
    finally:
        mod.config_dir = orig


def test_evolution_cycle_vibe_unavailable_state_unchanged(tmp_path):
    """Vibe check unavailable (timeout/failure) → slowdown state unchanged.

    Conservative: a failed question must not cause a wrong throttle NOR a
    wrong restore (rant 2026-08-20T10:58:55)."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "self", "content": "Done", "done": True,
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
        {"request_id": "self", "content": "Done", "done": True,
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
        {"request_id": "self", "content": "Done", "done": True,
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
        h1 = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        h1._slowdown_active = True
        h1._save_saturation_state()
        # "daemon restart": a fresh handler over the same config_dir
        h2 = make_handler(name="emrg-task", config={}, interval=60, identity=InstanceIdentity())
        assert h2._slowdown_active is True, \
            "throttled state restored from disk"
        # old-format file (no slowdown_active) reads as False — no migration
        (tmp_path / "saturation" / "other.json").write_text(
            '{"empty_cycles": 3, "slowdown_hits": 2}', encoding="utf-8")
        h4 = make_handler(name="other", config={}, interval=60, identity=InstanceIdentity())
        assert h4._slowdown_active is False, \
            "legacy saturation files simply read as not throttled"
    finally:
        mod.config_dir = orig


# ── Cycle progress heartbeat (rant 2026-08-25T09:25:32 ③) ─────
# While a cycle runs, tool frames mirror round/tool_count into _cycle_progress
# and write <task>.heartbeat.json (status=running) — a daemon killed mid-cycle
# leaves the marker behind, and the next handler start reports exactly where
# the cycle was interrupted (silent-death incidents diagnosable from
# emrgd.log alone).

def test_cycle_heartbeat_tracks_progress_and_clears(tmp_path):
    """Tool frames update _cycle_progress + write the running heartbeat; a
    clean cycle end removes the marker (run() finally path)."""
    import json as _json

    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"tool_name": "bash"},
        {"tool_name": "read"},
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "vibe_check_result", "ok": True,
         "result": {"work": "", "recommend_slowdown": False,
                    "slowdown_reason": ""}},
    ])
    asyncio.run(handler._run_evolution_cycle())
    # in-memory progress tracked from the streamed frames
    assert handler._cycle_progress["tool_count"] == 2
    assert handler._cycle_progress["cycle_started_at"] is None or \
        handler._cycle_progress["cycle_started_at"]
    # heartbeat file was written on the last tool frame (status=running)
    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert hb.exists(), "heartbeat file must be written during the cycle"
    data = _json.loads(hb.read_text(encoding="utf-8"))
    assert data["status"] == "running"
    assert data["task"] == "emrg-task"
    assert data["tool_count"] == 2
    assert data["last_heartbeat_at"], "heartbeat must carry a timestamp"
    assert "round" not in data, (
        "the round field is gone: no daemon frame carries one, so it could only "
        "ever be written as 0 (rant 2026-09-27T19:45:54, item 3)"
    )
    # clean cycle end removes the running marker (run() finally → _end_heartbeat)
    handler._end_heartbeat(handler._CLEAN_END)
    assert not hb.exists(), "clean cycle end must remove the heartbeat marker"


def test_cycle_heartbeat_interrupted_reported_on_restart(tmp_path, caplog):
    """A heartbeat left with status=running (daemon killed mid-cycle) is
    reported by the next handler start: progress restored into
    _cycle_progress + a warning naming the ending and the last progress."""
    import json as _json

    from emrg.server import scheduler as mod

    hb_dir = tmp_path / "logs" / "task-runs"
    hb_dir.mkdir(parents=True)
    (hb_dir / "emrg-task.heartbeat.json").write_text(
        _json.dumps({
            "task": "emrg-task", "status": "running",
            "cycle_started_at": "2026-08-25T00:00:00",
            "last_heartbeat_at": "2026-08-25T00:05:00",
            "tool_count": 7,
        }), encoding="utf-8")
    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
            h = make_handler(name="emrg-task", config={}, interval=60,
                            identity=InstanceIdentity())
    finally:
        mod.config_dir = orig
    # progress restored (informational; the next cycle resets it at start)
    assert h._cycle_progress["tool_count"] == 7
    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("previous cycle was killed mid-cycle" in m for m in warnings), warnings
    assert any("tool_count=7" in m for m in warnings), warnings
    # reported once: the record is consumed, so a later cycle cannot report it
    # again or half-overwrite it
    assert not (hb_dir / "emrg-task.heartbeat.json").exists(), (
        "the reported record must be consumed"
    )


def test_cycle_cancelled_by_shutdown_leaves_a_record(tmp_path):
    """A cycle cancelled by shutdown keeps its marker instead of erasing it.

    Rant 2026-09-27T19:45:54 (defect A). `stop_all()` cancels the handler
    coroutine, and `CancelledError` is not an `Exception`, so the old
    `except Exception` never saw the ending while the `finally` unlinked the only
    record of it — measured 2026-09-27: a cycle wedged 45 minutes, a host
    restart, and no line in emrgd.log saying the previous cycle had ended.
    """
    import json as _json

    handler, _ = _make_cycle_handler(tmp_path, frames=[])

    async def cancelled():
        raise asyncio.CancelledError()

    handler._run_evolution_cycle = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(handler._run_cycle_bounded())

    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert hb.exists(), "a cancelled cycle must not erase its progress record"
    data = _json.loads(hb.read_text(encoding="utf-8"))
    assert data["status"] == "shutdown-cancelled", data
    assert data["ended_at"], "the record must say when it ended"
    assert data["cycle_started_at"], "the record must say when the cycle started"
    assert handler._cycle_running is False, "the cycle flag must still be released"


def test_cycle_crash_leaves_a_record(tmp_path):
    """A crash is a distinct ending and says so (not a clean end)."""
    import json as _json

    handler, _ = _make_cycle_handler(tmp_path, frames=[])

    async def boom():
        raise RuntimeError("the cycle died")

    handler._run_evolution_cycle = boom
    asyncio.run(handler._run_cycle_bounded())

    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert hb.exists(), "a crashed cycle must leave a record"
    assert _json.loads(hb.read_text(encoding="utf-8"))["status"] == "crashed"
    assert handler._cycle_running is False, "the cycle flag must still be released"


def test_the_marker_exists_before_the_first_tool_frame(tmp_path):
    """The marker is written when the cycle starts, not on its first tool frame.

    A cycle can spend minutes in its first LLM round. While the marker was
    written only on a tool frame, a daemon killed in that window left no marker
    at all — indistinguishable from a clean end, which is the one state this
    file exists to tell apart. Driven by a cycle that inspects the file from
    inside, so the assertion is about the state during the cycle, not after it.
    """
    import json as _json

    handler, _ = _make_cycle_handler(tmp_path, frames=[])
    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    seen: dict = {}

    async def peek():
        seen["exists"] = hb.exists()
        seen["status"] = _json.loads(hb.read_text(encoding="utf-8"))["status"] \
            if hb.exists() else None

    handler._run_evolution_cycle = peek
    asyncio.run(handler._run_cycle_bounded())
    assert seen.get("exists") is True, (
        "the marker must exist from the first instant of the cycle"
    )
    assert seen.get("status") == "running", seen


def test_cycle_clean_end_leaves_no_record(tmp_path):
    """The other direction: a finished cycle leaves nothing to report.

    Without this, "always leave a record" would satisfy the tests above while
    every ordinary cycle (the overwhelming majority) left a false ending behind
    for the next start to report.
    """
    handler, _ = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])
    asyncio.run(handler._run_cycle_bounded())
    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert not hb.exists(), "a clean end leaves no record"


def test_cycle_record_is_reported_once_with_its_reason(tmp_path, caplog):
    """The record a cancelled cycle left is reported by the next cycle's start.

    A record that only ever reached a restart would be erased by the next cycle
    of the same process (the marker is rewritten every cycle), so the consumption
    in `_report_interrupted_cycle` is load-bearing: it turns the file into a
    one-shot report rather than a slot the next cycle silently overwrites.
    """
    import json as _json

    hb_dir = tmp_path / "logs" / "task-runs"
    hb_dir.mkdir(parents=True)
    hb = hb_dir / "emrg-task.heartbeat.json"
    hb.write_text(_json.dumps({
        "task": "emrg-task", "status": "shutdown-cancelled",
        "cycle_started_at": "2026-09-27T18:56:22",
        "ended_at": "2026-09-27T19:41:57",
        "last_heartbeat_at": "2026-09-27T19:05:55",
        "tool_count": 159,
    }), encoding="utf-8")

    handler, _ = _make_cycle_handler(tmp_path, frames=[])
    with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
        handler._report_interrupted_cycle()

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("was cancelled by shutdown" in m for m in warnings), warnings
    assert any("tool_count=159" in m for m in warnings), (
        f"the report must carry the last progress: {warnings}"
    )
    assert not hb.exists(), "a reported record must not be left for a second read"
    with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
        caplog.clear()
        handler._report_interrupted_cycle()
    assert not caplog.records, "a consumed record must not be reported twice"


# ── A cycle that never sees a terminal frame (rant 2026-09-28T13:03:33) ──
# The connection closing is not the cycle saying it finished. Measured
# 2026-09-28: a cycle wedged for 31 minutes ended through a graceful daemon
# shutdown (the handler was blocked in `recv`, the server closed the socket),
# ran its normal tail, and was recorded as a completed evolution — the marker
# was unlinked, so the restart report had nothing to say. The three endings the
# earlier work covered (hard kill, cancellation, crash) all leave a trace; this
# one, the shape a wedged cycle actually ends in, did not.

def test_connection_closed_without_a_terminal_frame_keeps_its_record(tmp_path):
    """No terminal frame → the marker survives, naming why the cycle stopped.

    The scripted connection runs out of frames (the `_FakeWS` raises
    `ConnectionClosed`), which is exactly the shape of a daemon shutting down
    under a blocked handler: the loop breaks with no `done` frame ever seen.
    """
    import json as _json

    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"tool_name": "bash"},
        {"tool_name": "bash"},
    ])
    reason = asyncio.run(handler._run_cycle_bounded())

    assert reason == "connection-closed", (
        f"the cycle must report how it ended, got {reason!r}"
    )
    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert hb.exists(), (
        "a cycle that never saw a terminal frame must leave a record — it was "
        "unlinked here before, which is what made a wedged cycle read as a "
        "finished one"
    )
    data = _json.loads(hb.read_text(encoding="utf-8"))
    assert data["status"] == "connection-closed", data
    assert data["ended_at"], "the record must say when it ended"
    assert data["cycle_started_at"], "the record must say when it started"
    assert data["tool_count"] == 2, "the record must carry the last progress"
    assert handler._cycle_running is False, "the flag must still be released"


def test_connection_closed_without_a_terminal_frame_is_not_an_evolution(tmp_path):
    """The other half: a cycle with no terminal frame is not a completed one.

    Writing it would count work that never happened — the same reason an
    aborted cycle is not counted — and it would advance the task-run record the
    GUI reads as a finished cycle.
    """
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"tool_name": "bash"},
    ])
    handler._slowdown_active = True
    asyncio.run(handler._run_evolution_cycle())

    assert "log" not in captured, (
        "a cycle that never received a terminal frame must not be recorded as "
        "a finished evolution"
    )
    assert handler.evolutions == []
    assert handler._slowdown_active is True, (
        "no vibe signal arrived, so the slowdown state must be untouched"
    )


def test_connection_closed_ending_is_reported_by_the_next_start(tmp_path, caplog):
    """The record the ending leaves is reported on one grep-able line.

    End to end, through the same file: the cycle writes it, the next start
    reports and consumes it — so a host restart after a wedged cycle says where
    that cycle stopped instead of saying nothing.
    """
    handler, _ = _make_cycle_handler(tmp_path, frames=[{"tool_name": "bash"}])
    asyncio.run(handler._run_cycle_bounded())

    with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
        handler._report_interrupted_cycle()

    warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("ended without a terminal frame" in m for m in warnings), warnings
    assert any("tool_count=1" in m for m in warnings), warnings
    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert not hb.exists(), "the report consumes the record"


def test_a_cycle_that_never_started_leaves_no_record(tmp_path):
    """A connect failure is not an interrupted cycle and must not look like one.

    Nothing ran, so there is no progress to report; the failure has its own
    warning and its own escalation. Without this, every tick while the daemon is
    down would leave a marker for the next cycle to report.
    """
    from emrg.server import scheduler as mod

    handler, _ = _make_cycle_handler(tmp_path, frames=[])

    async def _refuse():
        raise ConnectionRefusedError("no daemon")

    original = mod.connect_to_server
    mod.connect_to_server = _refuse
    try:
        reason = asyncio.run(handler._run_cycle_bounded())
    finally:
        mod.connect_to_server = original

    assert reason == "not-started", f"got {reason!r}"
    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert not hb.exists(), (
        "a cycle that never reached the daemon leaves no marker: the connect "
        "failure is already logged and escalated"
    )


def test_a_terminal_frame_still_ends_cleanly(tmp_path):
    """The discriminating direction: a `done` frame is still a clean end.

    The rule added above is "no terminal frame ⇒ not finished"; a fix that
    turned every ending into an interrupted one would satisfy the tests over it
    while reporting a failure for every ordinary cycle.
    """
    handler, _ = _make_cycle_handler(tmp_path, frames=[
        {"tool_name": "bash"},
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])
    reason = asyncio.run(handler._run_cycle_bounded())

    assert reason == "done", f"got {reason!r}"
    hb = tmp_path / "logs" / "task-runs" / "emrg-task.heartbeat.json"
    assert not hb.exists(), "a finished cycle leaves nothing to report"


# ── Next-run persistence (rant 2026-08-25T09:25:32 ④) ─────────
# The scheduled next-run time persists to ~/.emrg/next-run/<task>.json so a
# daemon restart does not reset the schedule (only shortens the wait; a slot
# that passed during downtime runs immediately instead of a fresh interval).

def test_next_run_state_persisted_across_restart(tmp_path):
    """_next_run_at survives a daemon restart via the next-run file: a fresh
    handler over the same config_dir resumes the original slot one-shot,
    never extending the wait; stale/cleared states fall back to normal."""
    import json as _json
    import time as _time

    from emrg.server import scheduler as mod

    orig = mod.config_dir
    try:
        mod.config_dir = lambda: tmp_path
        h1 = make_handler(name="emrg-task", config={}, interval=60,
                         identity=InstanceIdentity())
        h1._next_run_at = _time.time() + 3600
        h1._save_next_run_state()
        nrf = tmp_path / "next-run" / "emrg-task.json"
        assert nrf.exists(), "next-run state must persist to disk"
        # "daemon restart": a fresh handler over the same config_dir restores
        # the persisted slot (one-shot, never extends the normal wait)
        h2 = make_handler(name="emrg-task", config={}, interval=60,
                         identity=InstanceIdentity())
        assert h2._resume_next_run_at is not None, \
            "future next-run slot restored from disk"
        resumed = h2._resume_wait_timeout(600)
        assert 0 < resumed <= 600, resumed
        assert h2._resume_wait_timeout(600) == 600, \
            "one-shot: the second wait falls back to the normal interval"
        # a slot that expired while the daemon was down → run immediately
        h3 = make_handler(name="emrg-task", config={}, interval=60,
                         identity=InstanceIdentity())
        h3._resume_next_run_at = _time.time() - 5  # passed during downtime
        assert h3._resume_wait_timeout(600) == 0.0, \
            "an already-passed slot runs immediately"
        # a stale file (slot in the past) is ignored entirely
        (tmp_path / "next-run" / "emrg-task.json").write_text(
            _json.dumps({"next_run_at": _time.time() - 60}), encoding="utf-8")
        h4 = make_handler(name="emrg-task", config={}, interval=60,
                         identity=InstanceIdentity())
        assert h4._resume_next_run_at is None, \
            "a past slot must not be resumed"
        # clearing the state deletes the persisted file
        h4._next_run_at = None
        h4._save_next_run_state()
        assert not nrf.exists(), "cleared state must delete the next-run file"
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
            return _FakeWsForCycle([{"request_id": "self", "content": "Done", "done": True,
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

    handler = make_handler(
        name="emrg-task", config={"project": "emrg"}, interval=60,
        identity=InstanceIdentity(),
    )
    handler._connect_failures = 0
    assert handler._connect_backoff() == 60.0


def test_connect_backoff_grows_exponentially_capped(tmp_path):
    """Consecutive failures grow the wait, capped at 10 minutes."""
    from emrg.server.scheduler import TaskHandler

    handler = make_handler(
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

    handler = make_handler(
        name="emrg-task", config={"project": "emrg"}, interval=10,
        identity=InstanceIdentity(),
    )
    handler._connect_failures = 2
    # max(30, 10 * 2^2) = max(30, 40) = 40
    assert handler._connect_backoff() == 40.0
    handler._connect_failures = 1
    # max(30, 10 * 2^1) = max(30, 20) = 30 → floor holds
    assert handler._connect_backoff() == 30.0


def _as_this_cycles_frame(frame, sent):
    """`"self"` in a frame's ids means *this cycle's* request — one home for it.

    The cycle's request id is `evolution-<the instant the cycle began>`, which a
    test cannot spell; the daemon's `id` is the one field that says whose a
    broadcast is (`daemon.py::_broadcast` reaches every subscriber of the
    session, the originator included, so the session's other requests land on
    this socket too). A harness with no way to say "mine" would make every test
    of that distinction unwritable — and a fixture that spells an id no daemon
    would ever put there tests a frame that never arrives, which is exactly the
    difference `_names_another_request` now turns into a verdict.

    The id is read from the first message the handler sent (its own `task`
    frame), not from the last: the vibe check sends on the same socket with no
    `id` of its own.
    """
    if not isinstance(frame, dict):
        return frame
    import json as _json

    mine = ""
    for raw in sent:
        try:
            data = _json.loads(raw)
        except Exception:  # noqa: BLE001 — a non-JSON payload says nothing here
            continue
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            mine = data["id"]
            break
    if not mine:
        return frame
    if frame.get("request_id") == "self":
        frame = {**frame, "request_id": mine}
    ids = frame.get("request_ids")
    if isinstance(ids, list) and "self" in ids:
        frame = {**frame,
                 "request_ids": [mine if i == "self" else i for i in ids]}
    return frame


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
            frame = _as_this_cycles_frame(self._frames.pop(0), self.sent)
            return self._json.dumps(frame, ensure_ascii=False)
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
    """heartbeat = max(interval, min(interval*8, 8h)); long intervals unchanged.

    The interval is read from the handler's record, so each row states it there — the
    one way to set a handler's cadence now that nothing is cached.
    """
    from emrg.server import scheduler as mod
    for interval, expected in [
        (1, 8),            # min*8 floor below the 8h cap
        (60, 480),         # emrg-task: 8 minutes
        (600, 4800),       # 10-min task: 80 minutes
        (3600, 28800),     # 1h task: 8h (cap)
        (14400, 28800),    # 4h task: min(115200, 28800) = 8h (cap)
        (28800, 28800),    # 8h task: unchanged (max keeps original)
        (86400, 86400),    # 24h task: unchanged (8x beyond cap → original)
    ]:
        handler = make_handler(interval=interval)
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
        {"request_id": "self", "content": "Done", "done": True,
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
        {"request_id": "self", "content": "Done", "done": True,
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


def test_list_tasks_logs_slow_status(tmp_path, caplog):
    """A slow `status()` surfaces as a WARNING naming the culprit.

    The timing probe is the guard the old "MUST stay pure in-memory" wording was
    standing in for, and it is unchanged: what it protects is the WS loop, so the rule
    is "no slow I/O" and this probe is how it is enforced (rant 2026-08-18T20:48:45).
    """
    import logging
    import time as _time
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, name="slow", project="", path=str(tmp_path))
    orig_status = handler.status

    def slow_status():
        _time.sleep(0.3)
        return orig_status()

    handler.status = slow_status
    sched = mod.TaskScheduler(InstanceIdentity())
    sched._tasks_file = tmp_path / "tasks.yml"
    sched._tasks_file.write_text(yaml.safe_dump([
        {"name": "slow", "type": "evolution", "config": {"project": "emrg"}, "enabled": True},
    ]))
    sched._live = {"slow": handler}
    with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
        tasks = sched.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["name"] == handler.name
    msgs = " ".join(r.message for r in caplog.records)
    assert "list_tasks took" in msgs, msgs
    assert ">200ms" in msgs, msgs
    assert handler.name in msgs, "per-handler breakdown must name the slow handler"


def test_list_tasks_reads_the_record_for_the_declared_fields(tmp_path):
    """type / enabled / config / sandbox come from the record, not from a start copy.

    The GUI tasks panel renders type badges and enabled hints and the edit form
    prefills from these fields (R2245). They used to be merged from `_handler_cfgs` —
    a copy of the record taken when the handler was started, which is why editing a
    task in the file left the panel showing the old values.
    """
    from emrg.server.scheduler import TaskScheduler

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tmp_path / "tasks.yml"
    sched._tasks_file.write_text(yaml.safe_dump([
        {"name": "journal", "type": "journal",
         "config": {"project": "sci", "repo": "argszero/sci"},
         "interval": 3600, "enabled": False, "sandbox": "read-only"},
    ]))

    tasks = sched.list_tasks()

    assert len(tasks) == 1
    row = tasks[0]
    assert row["name"] == "journal"
    assert row["type"] == "journal"
    assert row["enabled"] is False
    assert row["config"] == {"project": "sci", "repo": "argszero/sci"}
    assert row["sandbox"] == "read-only"
    assert row["running"] is False, "no handler is live for it yet"
    assert row["interval"] == 3600


def test_list_tasks_shows_a_deleted_task_gone_at_once(tmp_path):
    """A record deleted from the file stops being listed immediately.

    The file is the skeleton of the listing, so a deletion is visible at the next
    list rather than when the (still running) handler next wakes.
    """
    from emrg.server.scheduler import TaskScheduler

    sched = TaskScheduler(InstanceIdentity())
    sched._tasks_file = tmp_path / "tasks.yml"
    sched._tasks_file.write_text(yaml.safe_dump([
        {"name": "keep", "type": "evolution", "config": {"project": "emrg"}},
        {"name": "drop", "type": "evolution", "config": {"project": "emrg"}},
    ]))
    assert [t["name"] for t in sched.list_tasks()] == ["keep", "drop"]

    sched.task_delete("drop")

    assert [t["name"] for t in sched.list_tasks()] == ["keep"]


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


def test_a_record_the_file_carries_runs_even_with_uppercase_in_its_name(tmp_path):
    """The loader must not refuse the content of its own file.

    `OfficeCLI-opensource-task` is the host's real record, named after the project
    `OfficeCLI` in projects.yml. While the name rule accepted lowercase only, the loader
    refused it at **every** scheduler tick — measured 2026-09-30: one identical error
    line ×3963 in `~/.emrg/emrgd.log`, first at 06:51:45 and still firing at 11:19:11, so
    the task did not run all morning while its record still read `enabled: true`.

    The guard sits on the seam that broke, `_read_own_record` — the one the scheduler
    calls at each wake — because that is what turned the rule into a dead task. Its
    second half pins the other direction: a name that is *not* path-safe is still
    refused, so widening the character class did not widen what may reach the filesystem.
    """
    from emrg.server import scheduler as mod
    from emrg.server.scheduler import write_table

    mod, orig = _p2_env(tmp_path)
    try:
        tasks_yml = tmp_path / "tasks.yml"
        host_record = {
            "name": "OfficeCLI-opensource-task",
            "type": "open-source",
            "sandbox": "workspace-write",
            "config": {"project": "OfficeCLI", "role": "contributor"},
            "interval": 3600,
            "enabled": True,
        }
        write_table([host_record], tasks_yml)

        handler = TaskHandler(name="OfficeCLI-opensource-task", identity=InstanceIdentity())
        handler._tasks_file = tasks_yml
        record = handler._read_own_record()
        assert record is not None, "the loader refused a record its own file carries"
        assert record["name"] == "OfficeCLI-opensource-task"

        # The other direction: path traversal is still not a task name.
        write_table([dict(host_record, name="../escape")], tasks_yml)
        unsafe = TaskHandler(name="../escape", identity=InstanceIdentity())
        unsafe._tasks_file = tasks_yml
        assert unsafe._read_own_record() is None
    finally:
        mod.config_dir = orig


def test_the_name_rule_is_about_the_path_not_the_case(tmp_path):
    """It refuses what could escape a directory, and nothing else.

    The task name is a file name (`task-runs/<name>.jsonl`, `<name>.heartbeat.json`,
    `next-run/<name>.json`) and a session id (`emrg-evolution-{name}`), so separators,
    traversal, spaces, control characters and over-length are unsafe. Case is not: it
    was never a safety property, and treating it as one killed a live host task.
    """
    from emrg.server.scheduler import TASK_NAME_MAX, validate_task_record

    def record(name: str) -> dict:
        return {"name": name, "type": "evolution", "interval": 60, "config": {}}

    # Accepted — including the host's own name and any other real project name.
    assert validate_task_record(record("OfficeCLI-opensource-task")) is None
    assert validate_task_record(record("MixedCase-Task")) is None
    assert validate_task_record(record("a" * TASK_NAME_MAX)) is None

    # Refused — each would escape a directory, or is not a name at all.
    for bad in [
        "",
        "Bad Name",
        "../escape",
        "a/b",
        "a\\b",
        "-lead",
        ".hidden",
        "emoji🙂",
        "a" * (TASK_NAME_MAX + 1),
    ]:
        assert validate_task_record(record(bad)) is not None, bad


def test_task_names_differing_only_in_case_are_the_same_task(tmp_path):
    """`Foo` and `foo` are one file and one session id on a case-insensitive filesystem.

    Allowing uppercase made this collision reachable, so the uniqueness check — which
    lives at the write boundary, where a name is chosen — compares case-insensitively.
    """
    from emrg.server import scheduler as mod

    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        ok, _ = sched.task_create("daily", "evolution", "mem", 300)
        assert ok
        ok, err = sched.task_create("Daily", "evolution", "mem", 300)
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


def test_reconcile_starts_what_is_missing_and_cancels_nothing(tmp_path):
    """reconcile adds the records that have no handler, and only ever adds.

    The removal and restart halves of the old `apply_tasks` are gone by construction
    (design acceptance I1): a config change is not an event, because the handler reads
    its own record at every wake. So this asserts the two things that replaced them —
    a deleted record stops being started, and the handler already running for a
    changed record is left alone.
    """
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        sched._write_table([
            {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 300, "enabled": True},
            {"name": "b", "type": "evolution", "config": {"project": "mem"}, "interval": 300, "enabled": True},
        ])

        async def _run():
            first = sched.reconcile()
            running_a = sched._live["a"]
            # b is deleted and c added; a's interval changes.
            sched._write_table([
                {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 900, "enabled": True},
                {"name": "c", "type": "evolution", "config": {"project": "mem"}, "interval": 900, "enabled": True},
            ])
            second = sched.reconcile()
            # Nothing has yielded yet, so no handler has woken: this is the live set
            # exactly as reconcile left it.
            live_after = dict(sched._live)
            # D3: the changed interval lands at the next cycle boundary, not by
            # rebuilding the handler — so drive the handler's own record read and see
            # the new value arrive there. Read the live set inside the loop: a handler
            # removes itself when its coroutine ends, and asyncio.run cancels
            # everything on its way out.
            await running_a._refresh_record()
            live = dict(sched._live)
            sched.stop_all()
            return first, second, running_a, live_after, live

        first, second, running_a, live_after, live = asyncio.run(_run())
        assert first["started"] == ["a", "b"]
        assert second["started"] == ["c"], "only the new record is started"
        assert live_after["a"] is running_a, (
            "an interval change must not rebuild the running handler — that was the "
            "silent cancel this design removes"
        )
        assert "b" in live_after, "a deleted record's handler exits at its next wake, not here"
        assert live["a"].interval == 900, "the new interval is read from the record"
    finally:
        mod.config_dir = orig


def test_reconcile_is_idempotent(tmp_path):
    """Reconciling an unchanged table starts nothing the second time."""
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        sched._write_table([
            {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 300, "enabled": True},
        ])

        async def _run():
            sched.reconcile()
            second = sched.reconcile()
            live = sorted(sched._live)
            sched.stop_all()
            return second, live

        second, live = asyncio.run(_run())
        assert second == {"started": []}
        assert live == ["a"]
    finally:
        mod.config_dir = orig


def test_reconcile_changes_nothing_when_the_table_is_unreadable(tmp_path):
    """Acceptance I2: an unreadable table is zero action, never "the table is empty".

    Reading it as empty would retire every handler at its next wake — i.e. stop every
    scheduled task after one non-atomic save by an editor. This is the failure mode the
    whole change is organised around, so the arm here is the reading, not a comment.
    """
    from emrg.server import scheduler as mod
    mod, orig = _p2_env(tmp_path)
    try:
        sched = TaskScheduler(InstanceIdentity())
        sched._tasks_file = tmp_path / "tasks.yml"
        sched._write_table([
            {"name": "a", "type": "evolution", "config": {"project": "emrg"}, "interval": 300, "enabled": True},
        ])

        async def _run():
            sched.reconcile()
            running = dict(sched._live)
            sched._tasks_file.write_text("a: [unclosed\n")
            result = sched.reconcile()
            still = dict(sched._live)
            sched.stop_all()
            return running, result, still

        running, result, still = asyncio.run(_run())
        assert result == {"started": [], "unreadable": True}
        assert still == running, "nothing was started, stopped or rebuilt"
    finally:
        mod.config_dir = orig


def test_the_record_read_is_what_leaves_the_event_loop():
    """rant 2026-08-19T01:05:47 — the git probe must not run on the loop.

    Deriving a record probes the project's git remote (a subprocess), and that now
    happens at every wake inside `run()` rather than once in `__init__` — so the
    offload moved with it and has to be asserted where it now is. Reading a source
    string is the check because the alternative is a slow git on the loop, which this
    suite cannot observe without one.
    """
    import inspect

    from emrg.server import scheduler as mod

    src = inspect.getsource(mod.TaskHandler)
    assert "record = await asyncio.to_thread(self._read_own_record)" in src
    assert "await asyncio.to_thread(self._apply_record, record)" in src


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
        assert templates["evolution"]["prompt"] and "{{ repo_url }}" in templates["evolution"]["prompt"]
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
            sched._write_table([task])
            sched.load_and_start()
            h = sched._live["daily-report"]
            # The handler derives its template from its own record at the top of its
            # loop (design §4: the record's `type` picks the template), so drive that
            # read rather than assuming it has already happened.
            await h._refresh_record()
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
        instance_id="test", host_name="host", repo_url="https://github.com/argszero/emrg.git", owner="argszero",
        repo="emrg", local_source="/tmp/evo", source_dir="/tmp/evo",
        session_id="s1", timestamp="20260817",
        task={"role": "committer", "project": "emrg"},
        project={}, git_path="git", gh_path="gh",
    )
    assert "emrg" in out, "task.project 值应渲染"
    assert "argszero/emrg" in out, "owner/repo 形式应渲染"
    assert "ignore rants without a `project` field entirely" in out


# ── sandbox tier resolution (rant 2026-08-20T18:05:20) ─────────────


def test_sandbox_resolution_unified_default_rule():
    """Rant 2026-08-20T18:05:20: no name-based builtin defaults — the
    configured value wins, otherwise the unified default is workspace-write.
    There is no implicit danger-full-access fallback."""
    # explicit tasks.yml top-level sandbox wins
    assert TaskHandler._resolve_sandbox({}, "read-only") == "read-only"
    # config.sandbox used when no explicit value
    assert TaskHandler._resolve_sandbox({"sandbox": "read-only"}, None) == "read-only"
    # explicit beats config
    assert (
        TaskHandler._resolve_sandbox({"sandbox": "workspace-write"}, "danger-full-access")
        == "danger-full-access"
    )
    # no name-based defaults: emrg-task no longer implies workspace-write
    # via the old special case — it is the unified default now
    handler = make_handler(
        name="emrg-task", config={"project": "emrg"}, interval=60,
        identity=InstanceIdentity(),
    )
    assert handler._sandbox == "workspace-write"
    # plain task name also gets the unified default, not danger-full-access
    handler2 = make_handler(
        name="generic-task", config={}, interval=60,
        identity=InstanceIdentity(),
    )
    assert handler2._sandbox == "workspace-write"
    # invalid values fall through to the default
    assert TaskHandler._resolve_sandbox({"sandbox": "bogus"}, None) == "workspace-write"
    assert TaskHandler._resolve_sandbox({}, "bogus") == "workspace-write"


# ── structural dirty-tree guard (community issue #979) ────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]

# The verdicts that hold only while `EMRG_TASK_DIRTY_OVERRIDE` is *absent*
# (issue #1326); the subprocess test below runs them with it exported.
#
# What the override replaces changed with the pinning contract (2026-09-23). It used
# to decide the *tier*: unique dirt forced `read-only`, the override handed the
# configured tier back, and every read-only verdict was override-sensitive. A pinned
# tree keeps its tier either way, so the tier is no longer a discriminator — what the
# early return skips is the **convergence** itself. These are the tests that assert
# the tree was converged and pinned, which is exactly what an exported override stops
# happening: with it, `_effective_sandbox` returns before the recovery is reached.
#
# Two, not three: `test_the_pinned_ref_alone_restores_the_work` drives
# `_recover_dirty_tree_sync` directly, so it never reads the variable. Measured by
# neutering the fixture below — it passed while the other two failed. A guard whose
# expected count names a test that cannot fail is the same defect as a guard that
# cannot fail.
OVERRIDE_SENSITIVE_TESTS = (
    "tests/test_scheduler.py::test_reconstructible_dirt_is_recovered_by_the_daemon_itself",
    "tests/test_scheduler.py::test_a_unique_tree_is_pinned_and_the_cycle_keeps_its_tier",
)


@pytest.fixture(autouse=True)
def _no_ambient_dirty_override(monkeypatch):
    """The caller's shell must not decide a dirty-tree verdict (issue #1326).

    `_effective_sandbox` reads `EMRG_TASK_DIRTY_OVERRIDE` from the process
    environment and hands back the *configured* tier when the task is named, so
    an exported variable replaces the verdict the tests below assert. That
    variable is precisely how an evolution cycle keeps working on a dirty tree,
    which is why the standard verification command reported
    `4 failed` on a tree where the same tests pass with it unset (measured
    2026-09-17 on a dirty main tree): the variable decided, not the dirt.

    A test's premise is its own: this module builds the trees it measures, so
    the ambient variable is removed here rather than inherited. The one test
    that *means* to exercise the override
    (`test_dirty_tree_override_env_audited_receipt`) sets it itself, which is
    why this is an autouse clear rather than three separate requests.
    """
    monkeypatch.delenv("EMRG_TASK_DIRTY_OVERRIDE", raising=False)


def test_the_dirty_tree_verdicts_survive_an_exported_override():
    """The CI-visible half: the convergence verdicts hold in a subprocess that
    exports `EMRG_TASK_DIRTY_OVERRIDE`, so the fixture above is the reason they hold.

    In CI the variable is never exported, so the tests pass with or without the
    fixture — the run below is what keeps the insulation from being deleted
    silently. Its failure mode without the fixture is the class measured for
    issue #1326, a caller's convenience variable deciding a cycle's verdict; the
    count is the one this set has now (measured 2026-09-23 by neutering the
    fixture: `2 failed, 1 passed`).

    The export is this guard's whole power, so it is pinned rather than assumed.
    Dropping it — `env = dict(os.environ)` — leaves the child reporting
    `2 passed` whatever the module does, and then the guard passes in *both*
    states, insulation present or deleted (measured 2026-09-17 on the merged
    tree: `1 passed` either way). A guard that cannot fail reads like a guard
    that passes, which is the one failure this file exists to prevent in the
    module under it, so the probe below observes the variable from the child
    side — a child process, not a second look at the same dict.
    """
    env = dict(os.environ, EMRG_TASK_DIRTY_OVERRIDE="emrg-task")
    probe = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; "
         "sys.exit(0 if os.environ.get('EMRG_TASK_DIRTY_OVERRIDE') == 'emrg-task' else 1)"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert probe.returncode == 0, (
        "the child run below must be handed EMRG_TASK_DIRTY_OVERRIDE, or it "
        "cannot fail and this guard's verdict means nothing"
    )
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *OVERRIDE_SENSITIVE_TESTS],
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"{len(OVERRIDE_SENSITIVE_TESTS)} passed" in out.stdout, out.stdout + out.stderr


def test_is_dirty_tree_detects_uncommitted_changes():
    """Community issue #979: a git repo with uncommitted changes is detected
    via `git status --porcelain`; a clean repo and a non-git dir are not
    (fail-open — the guard never blocks a cycle by itself)."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", d], capture_output=True, timeout=10)
        assert TaskHandler._is_dirty_tree_sync(d) is False  # clean repo
        (Path(d) / "x.txt").write_text("hi", encoding="utf-8")
        assert TaskHandler._is_dirty_tree_sync(d) is True  # untracked → dirty
    # non-git dir → fail-open (False)
    with tempfile.TemporaryDirectory() as d:
        assert TaskHandler._is_dirty_tree_sync(d) is False


def test_is_dirty_tree_linked_worktree():
    """Cycle 20260825-194513: a linked git worktree has `.git` as a FILE, not
    a dir — the probe must detect dirt there too (isdir-only check silently
    failed open, bypassing the read-only guard in worktree setups)."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", d], capture_output=True, timeout=10)
        subprocess.run(["git", "-C", d, "config", "user.email", "t@t.t"],
                       capture_output=True, timeout=10)
        subprocess.run(["git", "-C", d, "config", "user.name", "t"],
                       capture_output=True, timeout=10)
        (Path(d) / "base.txt").write_text("base", encoding="utf-8")
        subprocess.run(["git", "-C", d, "add", "base.txt"], capture_output=True, timeout=10)
        subprocess.run(["git", "-C", d, "commit", "-m", "base"],
                       capture_output=True, timeout=10)
        wt = Path(d) / "wt"
        subprocess.run(["git", "-C", d, "worktree", "add", str(wt), "HEAD"],
                       capture_output=True, timeout=10)
        # .git is a file in the linked worktree
        assert (wt / ".git").is_file(), "linked worktree .git should be a file"
        assert TaskHandler._is_dirty_tree_sync(str(wt)) is False  # clean worktree
        (wt / "dirty.txt").write_text("dirty", encoding="utf-8")
        assert TaskHandler._is_dirty_tree_sync(str(wt)) is True  # untracked → dirty


def test_a_unique_tree_is_pinned_and_the_cycle_keeps_its_tier(tmp_path):
    """Host directive 2026-09-23 (#1465): a dirty tree cleans itself up.

    This test used to be `test_dirty_tree_forces_read_only_structural_guard`, and it
    stated the *reason* by injecting `loses_unique=True` into a handler pointing at
    the repository under test. Both halves of that shape were wrong:

    * the verdict is now measured, never supplied (see
      `test_no_caller_can_supply_a_verdict`), so the tree is built here instead —
      an untracked file, which is the one shape that exists nowhere else;
    * the outcome inverted. Unique dirt no longer costs the cycle its tier: the
      criterion's claim is a claim about *reachability*, a ref is reachability, so the
      daemon pins the work under `refs/emrg/rescue/` and the claim stops being true.
      Forcing `read-only` is what made the state absorbing — read-only refuses the very
      git verbs that would have converged the tree, so 10/10 mutators were blocked and
      the state could only be left by a human.
    """
    repo = _repo_with_dirt(tmp_path, "untracked")
    handler = make_handler(name="emrg-task", config={"path": repo}, interval=60,
                          identity=InstanceIdentity())
    assert handler._sandbox == "workspace-write"  # configured default

    assert asyncio.run(handler._effective_sandbox()) == "workspace-write", \
        "a pinned tree is not a tree the cycle has to be locked out of"
    assert _status(repo).strip() == "", "the daemon converges a unique tree itself too"
    assert _git_out(repo, "stash", "list") != "", "and it does so without discarding a byte"

    # Two refs, and they are the whole difference between "pinned" and "refused":
    # the stash commit carrying the worktree's bytes, and HEAD, which a stash cannot
    # move (the criterion's other clause is about a commit reachable from this
    # checkout alone).
    pins = sorted(r for r in _git_out(repo, "for-each-ref", "--format=%(refname)",
                                      "refs/emrg/rescue/").splitlines())
    assert len(pins) == 2, pins
    assert pins[1].endswith("-head"), pins

    git_dir = Path(_git_out(repo, "rev-parse", "--absolute-git-dir"))
    receipt = json.loads((git_dir / "emrg-recovery-receipt.json").read_text(encoding="utf-8"))
    assert receipt["rescued_unique"] is True
    assert receipt["rescue_ref"] in pins, receipt
    assert receipt["reversible_with"].startswith("`git stash apply --index "), receipt

    # configured read-only stays read-only (no weakening) — on a tree the same shape,
    # so the tier is the only difference between the two runs.
    other = tmp_path / "configured-read-only"
    other.mkdir()
    ro_repo = _repo_with_dirt(other, "untracked")
    ro_handler = make_handler(name="ro-task", config={"path": ro_repo}, interval=60,
                             identity=InstanceIdentity(), sandbox="read-only")
    assert asyncio.run(ro_handler._effective_sandbox()) == "read-only"


def test_no_caller_can_supply_a_verdict():
    """#1274's lesson, promoted from a comment to the signature.

    `_recover_dirty_tree_sync` stopped taking a caller's verdict because a guarantee
    that holds only while every caller passes the truth is not a guarantee. Its caller
    kept two such parameters (`dirty`, `loses_unique`) and paid the same price one
    level up, both directions measured on 2026-09-23:

    * `dirty=False` on a tree that is dirty **bypasses the guard** — the probe is
      skipped and the cycle gets its configured tier over an unconverged tree;
    * `dirty=True` is *destructive*: it drives a real `git stash push -u` against
      whatever repository the handler points at. Pointed at this repository, from a
      test, it moved an uncommitted fix out of the working tree.

    The probe and the criterion are cheap and the only production caller passed
    neither, so there is no verdict to supply any more.
    """
    assert list(inspect.signature(TaskHandler._effective_sandbox).parameters) == ["self"]


def _status(repo: str) -> str:
    """`git status --porcelain` in `repo`, as text."""
    return subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                          capture_output=True, text=True, timeout=30,
                          encoding="utf-8", errors="replace").stdout


def _git_out(repo: str, *args: str) -> str:
    """Stdout of `git <args>` in `repo`, stripped."""
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          timeout=30, encoding="utf-8", errors="replace").stdout.strip()


def _repo_with_dirt(tmp_path, kind: str) -> str:
    """A one-commit repo made dirty in a named way. Returns its path.

    `deleted` — a tracked file removed from the worktree: `git status` reports
    ` D`, and discarding restores it, so **nothing exists only here**.
    `untracked` — a file git has never seen: its bytes exist in exactly one place.
    """
    repo = tmp_path / f"repo-{kind}"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)],
                   capture_output=True, timeout=30)
    for key, value in (("user.email", "t@t.t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value],
                       capture_output=True, timeout=30)
    (repo / "f.txt").write_text("v1", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "base"],
                   capture_output=True, timeout=30)
    if kind == "deleted":
        (repo / "f.txt").unlink()
    else:
        (repo / "notes.md").write_text("only here", encoding="utf-8")
    return str(repo)


def test_reconstructible_dirt_is_recovered_by_the_daemon_itself(tmp_path):
    """Host directive 2026-09-16 (issue #1237): dirt must be *recovered from*.

    The downgrade is keyed on loss, and its exit is the daemon's own action -- not a
    command a human has to be present to run. A guard whose exit needs a human
    strands the tree, which is the shape of the loss that cost 33 consecutive
    zero-commit cycles: the tier that blocked the cycle was also the tier that
    blocked the repair of the thing blocking it.

    Measured end-to-end through the real probe and the real recovery: the tree is
    clean afterwards without anyone asking, HEAD never moved, the moved work is in the
    stash — restored with the receipt's own spelling, `git stash apply --index
    stash@{N}` for the `N` `git stash list` prints (a bare `git stash pop` is not the
    inverse: it leaves a staged change unstaged and consumes the stash; the selector is
    the list's ordinal because no `@{…}` form names a stash by message; issue #1284,
    measured by
    `tests/test_recover_worktree.py::test_the_advertised_selector_survives_a_later_stash`)
    — and a receipt in the git dir records what happened.
    """
    repo = _repo_with_dirt(tmp_path, "deleted")
    assert _status(repo).startswith(" D"), "precondition: deletion-only dirt"
    head = _git_out(repo, "rev-parse", "HEAD")

    handler = make_handler(name="emrg-task", config={"path": repo}, interval=60,
                          identity=InstanceIdentity())
    assert asyncio.run(handler._effective_sandbox()) == "workspace-write", \
        "dirt holding no unique work must not cost the cycle its tier"

    assert _status(repo).strip() == "", \
        "the daemon must converge a reconstructible tree itself, not ask a human to"
    assert _git_out(repo, "rev-parse", "HEAD") == head, "the recovery must not move HEAD"
    assert "emrg-recovery-" in _git_out(repo, "stash", "list"), \
        "the action must be reversible: the work lives in a stash"

    git_dir = Path(_git_out(repo, "rev-parse", "--absolute-git-dir"))
    receipt = json.loads((git_dir / "emrg-recovery-receipt.json").read_text(encoding="utf-8"))
    assert receipt["repo"] == repo
    assert receipt["head_before"] == receipt["head_after"] == head
    assert receipt["status_before"] == [" D f.txt"], receipt["status_before"]
    assert receipt["status_after"] == []
    assert receipt["stash_message"] in receipt["reversible_with"], \
        "the reversal route must name the stash that was made, not the newest one"
    assert "--index" in receipt["reversible_with"], \
        "a stash carries the index side; the inverse must restore it"

    # Reversibility is why the action is allowed at all: popping restores the exact
    # state the daemon moved aside -- here, the deletion itself, byte for byte.
    pop = subprocess.run(["git", "-C", repo, "stash", "pop"],
                         capture_output=True, text=True, timeout=30,
                         encoding="utf-8", errors="replace")
    assert pop.returncode == 0, pop.stderr
    assert _status(repo) == " D f.txt\n", "the stash must carry the exact dirt it moved"


def test_a_receipt_that_cannot_be_written_is_reported_not_silent(tmp_path, caplog):
    """Issue #1284: the audit half of a release must not read as plain success.

    `_write_recovery_receipt` returns `str | None` for exactly the failure it
    anticipates — and its value was **discarded at the only call site**, so with the
    path pre-created as a directory (`open(..., "w")` raises `OSError`) the action
    said "1 reconstructible change(s) stashed … HEAD unmoved" and nothing anywhere
    said the receipt was missing. The recovery itself is unaffected — best-effort by
    design, the stash is the durable record — so what is asserted is the *report*,
    which was the only thing wrong.

    Both arms, because the sentence has to be a discriminator and not a constant: the
    same call without the forced failure must say nothing about a missing receipt and
    must leave a file behind.
    """
    repo = _repo_with_dirt(tmp_path, "deleted")
    git_dir = Path(_git_out(repo, "rev-parse", "--absolute-git-dir"))
    (git_dir / "emrg-recovery-receipt.json").mkdir()  # the documented failure mode

    with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
        status, detail = TaskHandler._recover_dirty_tree_sync(repo)

    assert status == "recovered", detail
    assert "no receipt could be written" in detail, detail
    assert not (git_dir / "emrg-recovery-receipt.json").is_file()
    # The recovery is still a recovery: the tree converged and the work is in a stash.
    assert _status(repo).strip() == ""
    assert "emrg-recovery-" in _git_out(repo, "stash", "list")
    # …and the log carries it too, for the reader who never sees the action's detail.
    assert any("could not write the recovery receipt" in record.getMessage()
               for record in caplog.records), [r.getMessage() for r in caplog.records]

    control_root = tmp_path / "control"
    control_root.mkdir()
    control = _repo_with_dirt(control_root, "deleted")
    status2, detail2 = TaskHandler._recover_dirty_tree_sync(control)
    assert status2 == "recovered" and "no receipt could be written" not in detail2, detail2
    assert (Path(_git_out(control, "rev-parse", "--absolute-git-dir"))
            / "emrg-recovery-receipt.json").is_file()


def test_the_pinned_ref_alone_restores_the_work(tmp_path):
    """The claim "exists nowhere else" is answered by making it false (#1465).

    This test used to be `test_unique_dirt_still_forces_read_only`, asserting the
    protection *unchanged*: unique dirt bought `read-only` and the recovery never ran,
    so the host's unsaved work stayed put. What that bought in safety it paid for in
    reachability — the tier blocked the repair of the thing it blocked — and the work
    was no less alone for having been left there.

    So the assertion moved to the half the guard actually cares about: nothing is
    discarded *and* the bytes are reachable from a ref. The strong form is below —
    `refs/stash` is cleared entirely, so the stash-shaped objects are unreachable
    through every ordinary route, and the pin alone brings back the modification, the
    staged side and the untracked file.
    """
    repo = _repo_with_dirt(tmp_path, "untracked")
    head = _git_out(repo, "rev-parse", "HEAD")
    status, detail = TaskHandler._recover_dirty_tree_sync(repo)
    assert status == "recovered", detail

    rescue = _git_out(repo, "for-each-ref", "--format=%(refname)",
                      "refs/emrg/rescue/").splitlines()
    rescue = [r for r in rescue if not r.endswith("-head")]
    assert len(rescue) == 1, rescue

    # The pin is compared against the stash it names rather than trusted: a pin that
    # silently did not resolve is indistinguishable from no pin at all.
    assert _git_out(repo, "rev-parse", rescue[0]) == _git_out(repo, "rev-parse", "refs/stash")
    assert _git_out(repo, "rev-parse", rescue[0] + "-head") == head

    # Drop every ordinary route to the bytes.
    subprocess.run(["git", "-C", repo, "stash", "clear"], capture_output=True, timeout=30)
    assert _git_out(repo, "stash", "list") == "", "precondition: refs/stash is gone"

    apply = subprocess.run(
        ["git", "-C", repo, "stash", "apply", "--index", rescue[0]],
        capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
    )
    # The verdict is the state, not the exit code: git restores some geometries exactly
    # and still exits 1 (`tests/test_recover_worktree.py` measures the `D ??` one).
    assert (tmp_path / "repo-untracked" / "notes.md").read_text(encoding="utf-8") == "only here", apply.stderr
    assert "?? notes.md" in _status(repo)


def test_dirty_tree_override_env_audited_receipt(tmp_path, caplog):
    """Community issue #979: EMRG_TASK_DIRTY_OVERRIDE (comma-separated task
    names, or *) lets a human lift the guard — every exception is logged as a
    receipt. The override must name THIS task (or *) to apply.

    Built on a repository this test owns, because what the override buys is now
    visible in the tree rather than in the tier: a pinned tree keeps its tier either
    way, so the *effect* of the override is that the recovery did not run — the host's
    unsaved work is still exactly where the host left it.
    """
    repo = _repo_with_dirt(tmp_path, "untracked")
    handler = make_handler(name="emrg-task", config={"path": repo}, interval=60,
                          identity=InstanceIdentity())
    old = os.environ.get("EMRG_TASK_DIRTY_OVERRIDE")
    try:
        # task named in the override → the guard stands down, tree untouched
        os.environ["EMRG_TASK_DIRTY_OVERRIDE"] = "other-task,emrg-task"
        with caplog.at_level(logging.WARNING, logger="emrg.server.scheduler"):
            assert asyncio.run(handler._effective_sandbox()) == "workspace-write"
        assert _status(repo) == "?? notes.md\n", "an override moves nothing"
        assert _git_out(repo, "stash", "list") == ""
        assert any("read-only guard overridden" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]
        # wildcard → same
        os.environ["EMRG_TASK_DIRTY_OVERRIDE"] = "*"
        assert asyncio.run(handler._effective_sandbox()) == "workspace-write"
        # override for a different task → the guard still applies, and converges
        os.environ["EMRG_TASK_DIRTY_OVERRIDE"] = "other-task"
        assert asyncio.run(handler._effective_sandbox()) == "workspace-write"
        assert _status(repo).strip() == "", "a guard that applies converges the tree"
    finally:
        if old is None:
            os.environ.pop("EMRG_TASK_DIRTY_OVERRIDE", None)
        else:
            os.environ["EMRG_TASK_DIRTY_OVERRIDE"] = old


def test_clean_tree_keeps_configured_sandbox(tmp_path):
    """Community issue #979: a clean tree leaves the configured tier intact —
    no behavior change for the normal case, and nothing is stashed."""
    repo = _repo_with_dirt(tmp_path, "deleted")
    subprocess.run(["git", "-C", repo, "checkout", "--", "f.txt"],
                   capture_output=True, timeout=30)
    assert _status(repo).strip() == "", "precondition: a clean tree"

    handler = make_handler(name="emrg-task", config={"path": repo}, interval=60,
                          identity=InstanceIdentity())
    assert asyncio.run(handler._effective_sandbox()) == "workspace-write"
    assert _git_out(repo, "stash", "list") == "", "a clean tree is never asked about loss"


# ── No-progress watchdog (rant 2026-09-29T15:52:43, requirement 5) ──────
# A cycle that stops producing frames must not hold its slot. The wedge this
# closes was measured 2026-09-27: a command put in the background killed the
# whole turn, nothing was ever written to the socket again, and the handler sat
# in `recv` for the life of the daemon — `_cycle_running` true, `_next_run_at`
# None, the heartbeat file still ticking. The heartbeat is why the signal here
# is a *frame*: it is this handler's own timer and says nothing about the cycle.

def _silence_frames(handler, tmp_path, monkeypatch, frames, *, delays=None):
    """A handler whose connection delivers `frames`, then goes silent forever.

    Silence — not a close — is the subject: `ConnectionClosed` already had an
    answer (`connection-closed`), and it is the open-but-silent socket that had
    none. `delays` maps a frame index to how long the socket stays silent before
    that frame arrives, which is how a test puts a pause *inside* a step rather
    than at the end of the script.
    """
    import json as _json

    from emrg.server import scheduler as mod
    delays = delays or {}

    class _SilentWS:
        def __init__(self):
            self._frames = list(frames)
            self.sent = []
            self._index = 0
            self.consumed = 0

        async def send(self, msg):
            self.sent.append(msg)

        async def recv(self):
            if self._frames:
                pause = delays.get(self._index)
                if pause:
                    await asyncio.sleep(pause)
                self._index += 1
                self.consumed += 1
                frame = self._frames.pop(0)
                return _json.dumps(_as_this_cycles_frame(frame, self.sent),
                                   ensure_ascii=False)
            await asyncio.sleep(3600)  # open, and never another frame

        async def close(self):
            pass

    async def _fake_connect():
        ws = _SilentWS()
        # Kept on the handler so a test can read what the loop did with the
        # script: `consumed` is how many frames it took in. The silence bound is
        # about *this cycle's* liveness, so "how far into somebody else's stream
        # did it get before giving up" is a reading a test has to be able to
        # take — see `test_a_holding_turns_stream_does_not_extend_...`.
        handler._silence_ws = ws
        return ws

    handler._build_evolution_prompt = lambda: "test prompt"
    monkeypatch.setattr(mod, "connect_to_server", _fake_connect)
    return handler


def test_a_silent_turn_ends_as_a_stall_not_as_a_completion(tmp_path, monkeypatch, caplog):
    """Requirement 5.2: silence past the bound is an error, not a wait.

    The bound is shrunk so the test is fast; what the assertions are about is
    the shape, not the number: the cycle returns (rather than blocking forever),
    it says *why* it returned, and it is not counted as an evolution.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"request_id": "self", "content": "thinking", "done": False,
         "delta": True, "session_id": "s"},
    ])

    with caplog.at_level(logging.ERROR):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._STALLED, (
        "a turn that stops reporting must end as `stalled`, not as a completion "
        f"or as a closed connection — got {reason!r}"
    )
    assert handler.evolutions == [], (
        "an aborted cycle is not an evolution: counting it would inflate the "
        "growth card and the task-run record with work that never happened"
    )
    assert any("stalled: no frame for" in r.getMessage() for r in caplog.records), (
        "the abort must be logged at ERROR with the silence it measured: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_a_tool_call_is_bounded_by_its_own_declared_timeout(tmp_path, monkeypatch):
    """Requirement 5.1: the bound follows the *step*, so a long command survives.

    This is the assertion that keeps the watchdog from being a fixed recv
    timeout: the round bound is 50 ms here, and the call declares 3700s of its
    own. Only a watchdog that reads the call's own bound lets it finish — a flat
    bound kills every command longer than itself, which is exactly the failure
    the rant forbids.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    monkeypatch.setattr(mod, "_TOOL_SILENCE_GRACE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "tool_start", "tool_name": "bash",
         "arguments": {"command": "sleep 3000", "timeout": 3700}},
        {"type": "tool_end", "tool_name": "bash", "content": "ok", "error": False},
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ], delays={1: 0.3})

    reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._CLEAN_END, (
        "a 3700s command is silent for longer than the round bound by design — "
        f"the cycle must not be torn down for it, got {reason!r}"
    )
    assert len(handler.evolutions) == 1, "the cycle ran to completion"


def test_the_watchdog_bounds_are_read_from_the_call_and_never_less(tmp_path):
    """The arithmetic, stated directly, including the values that must not bound less.

    A tool call that declares no usable `timeout` still may not silence the
    socket for longer than the tool layer's own default — otherwise omitting the
    field would be a way to disable the watchdog.
    """
    from emrg.server import scheduler as mod

    declared = mod._tool_silence_seconds({"timeout": 3700})
    missing = mod._tool_silence_seconds({})
    bogus = mod._tool_silence_seconds({"timeout": object()})
    boolean = mod._tool_silence_seconds({"timeout": True})
    negative = mod._tool_silence_seconds({"timeout": -5})

    assert declared == 3700 + mod._TOOL_SILENCE_GRACE_SECONDS
    assert missing == mod._TOOL_SILENCE_DEFAULT_SECONDS + mod._TOOL_SILENCE_GRACE_SECONDS
    assert bogus == missing, "an unparsable value must fall back, not crash or unbind"
    assert boolean == missing, "`True` is not a timeout in seconds"
    assert negative == missing, (
        "a non-positive timeout is as unusable as an absent one — clamping it to "
        "the grace alone would fire the watchdog instantly on every such call"
    )
    assert mod._tool_silence_seconds({"timeout": "600"}) == (
        600 + mod._TOOL_SILENCE_GRACE_SECONDS
    ), "the tool layer reads a numeric string, so the watchdog must too"

    # The later bound wins, in both phases.
    assert mod._silence_deadline(0.0, 0.0, None) == mod._ROUND_SILENCE_SECONDS
    assert mod._silence_deadline(0.0, 0.0, 3700.0) == 3700.0
    assert mod._silence_deadline(0.0, 0.0, 1.0) == mod._ROUND_SILENCE_SECONDS


def test_a_stalled_cycle_frees_the_slot_and_records_why(tmp_path, monkeypatch, caplog):
    """Requirement 5.2: reset the handler, and leave the reason behind.

    `_run_cycle_bounded` is where the reset happens (`_cycle_running` back to
    False, marker closed with the ending). Driving it rather than the inner
    method is deliberate: the reset is not something the cycle does for itself.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"request_id": "self", "content": "thinking", "done": False,
         "delta": True, "session_id": "s"},
    ])
    handler._cycle_running = True

    reason = asyncio.run(handler._run_cycle_bounded())

    assert reason == handler._STALLED
    assert handler._cycle_running is False, (
        "the slot must come back: it is `_cycle_running` that decides whether the "
        "handler may start another cycle"
    )
    hb = handler._task_runs_dir / "emrg-task.heartbeat.json"
    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["status"] == "stalled", (
        "the ending is written into the marker rather than erased, so the next "
        f"wake can report it — got {data['status']!r}"
    )

    # A later wake reports it, once, in one grep-able phrase.
    with caplog.at_level(logging.WARNING):
        handler._report_interrupted_cycle()
    assert any("stalled (no frame past the bound" in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    assert not hb.exists(), "a reported marker is consumed, so it is reported once"


def test_a_cycle_queued_behind_a_busy_session_is_not_a_stall(tmp_path, monkeypatch, caplog):
    """A busy session's silence is not a wedge, and the difference is the ending.

    Measured 2026-10-02 (issue #1815): the competition task's cycle began while the
    host's own turn held that session, the daemon answered `task_queued`, and the
    marker it left read `stalled / tools=0` — the 600s round bound, exactly — for a
    turn that never began. Both silences look identical on the wire; the ending is
    what tells the next cycle whether to investigate a fault or nothing at all.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED, (
        "the daemon held this cycle's request instead of starting it, so the "
        f"silence is a busy session, not a turn that stopped reporting — got {reason!r}"
    )
    assert reason != handler._STALLED, "the two endings must not be the same string"
    assert handler.evolutions == [], (
        "a cycle that never ran is not an evolution, however it ended"
    )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "nothing is wedged here, so nothing may be logged as an error — an ERROR "
        f"for a busy session is the false alarm this ending removes: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("queued behind a busy session" in m for m in messages), messages
    assert any("position 1" in m for m in messages), (
        "the position the daemon named is the evidence this reading rests on: "
        f"{messages}"
    )


def test_a_queued_request_that_is_served_inside_the_wait_is_a_normal_cycle(
    tmp_path, monkeypatch,
):
    """The other direction: `task_queued` is not a verdict on the cycle.

    The queue can drain inside the same wait — that is what the pending injection
    is for — and a cycle whose own turn then reports must end the way any cycle
    ends. A flag set by the first frame and never cleared would turn every such
    cycle into a `queued` ending, which is the same defect mirrored.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
        {"type": "steer_committed", "request_id": "self", "session_id": "s"},
        {"type": "tool_start", "tool_name": "bash", "arguments": {"command": "true"}},
        {"type": "tool_end", "tool_name": "bash", "content": "ok", "error": False},
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])

    reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._CLEAN_END, (
        "a cycle that started after being queued is an ordinary completed cycle "
        f"— got {reason!r}"
    )
    assert len(handler.evolutions) == 1, "it ran, so it counts"


def test_silence_after_a_queued_cycle_started_is_a_stall_again(tmp_path, monkeypatch, caplog):
    """The flag belongs to the wait, not to the cycle.

    A request that was queued and then served is a running turn, and a running
    turn that goes silent is the stall the watchdog exists for. Without the
    clearing, one busy session relabels every later silence this cycle meets —
    the same defect mirrored, and a wedge would be filed as a busy session.

    The frames carry this cycle's request id, because that is what the daemon
    sends: the turn's own `tool_start`/`tool_end` name the request they belong to
    (`daemon.py`), and it is the id — not the mere arrival of a frame — that says
    this turn is the cycle's own.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
        {"type": "tool_start", "request_id": "self", "tool_name": "bash",
         "arguments": {"command": "true"}},
        {"type": "tool_end", "request_id": "self", "tool_name": "bash",
         "content": "ok", "error": False},
    ])

    with caplog.at_level(logging.ERROR):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._STALLED, (
        "these frames are this cycle's own turn reporting, so the silence after "
        f"them is a wedged turn, not a busy session — got {reason!r}"
    )
    assert any("stalled: no frame for" in r.getMessage() for r in caplog.records), (
        f"a wedged turn is logged at ERROR: {[r.getMessage() for r in caplog.records]}"
    )


def test_a_queued_cycle_leaves_a_marker_that_names_the_busy_session(
    tmp_path, monkeypatch, caplog,
):
    """The marker and the restart report must not call it a stall.

    The ending is written into the marker rather than erased, and a later wake
    prints it in one grep-able phrase — so the phrase is part of the reading, not
    a log detail: a host reading `stalled` here would go looking for a wedge that
    does not exist.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 2},
    ])
    handler._cycle_running = True

    reason = asyncio.run(handler._run_cycle_bounded())

    assert reason == handler._QUEUED
    hb = handler._task_runs_dir / "emrg-task.heartbeat.json"
    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["status"] == "queued", (
        f"the marker must carry the ending, not the stall's — got {data['status']!r}"
    )

    with caplog.at_level(logging.WARNING):
        handler._report_interrupted_cycle()
    messages = [r.getMessage() for r in caplog.records]
    assert any("was queued behind a busy session" in m for m in messages), messages
    assert not any("stalled (no frame past the bound" in m for m in messages), (
        f"the stall's phrase must not appear for a queued cycle: {messages}"
    )
    assert not hb.exists(), "a reported marker is consumed, so it is reported once"


def test_a_stalled_cycle_does_not_hold_the_task_slot_forever(tmp_path, monkeypatch):
    """Requirement 5.2, end to end: the next run is scheduled again.

    Only the run loop writes the next-run slot, and it writes one only after a
    cycle returns — so a next-run file that appears *after* a stalled cycle is
    the deadlock being closed, measured rather than argued.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    mod.write_table(
        [{"name": "emrg-task", "type": "evolution", "config": {},
          "interval": 60, "enabled": True}],
        tmp_path / "tasks.yml",
    )
    handler = make_handler(name="emrg-task", config={}, interval=60)
    _silence_frames(handler, tmp_path, monkeypatch, frames=[])
    # `_silence_frames` replaces the connect call; the prompt builder must not
    # reach a template file that this record's type has no reason to resolve.
    handler._build_evolution_prompt = lambda: "test prompt"

    async def _drive():
        task = asyncio.create_task(handler.run())
        next_run = tmp_path / "next-run" / "emrg-task.json"
        try:
            # The slot is written *before* the loop waits, so it appears as soon
            # as the stalled cycle has returned — the wait itself is never entered.
            for _ in range(200):  # ≤ 4s: the cycle stalls in 50 ms
                await asyncio.sleep(0.02)
                if next_run.exists():
                    return json.loads(next_run.read_text(encoding="utf-8"))
            return None
        finally:
            handler.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    scheduled = asyncio.run(_drive())

    assert scheduled is not None, (
        "after a stalled cycle the handler must schedule another run — a task "
        "whose next-run slot is never written is the wedge this requirement closes"
    )
    assert scheduled["next_run_at"] > 0


def test_a_silent_daemon_after_the_terminal_frame_does_not_wedge_the_handler(
    tmp_path, monkeypatch, caplog,
):
    """The same wedge, one step later: the vibe check had no bound at all.

    Its recv loop carried no timeout on purpose (rant 2026-08-20T20:19:31), and
    an open, silent socket is not `ConnectionClosed` — so the one failure the
    watchdog exists for stayed reachable *after* the cycle had already looked
    finished, holding the slot just as long.
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._CLEAN_END, (
        "the terminal frame did arrive, so this cycle did finish — an unhelpful "
        f"vibe check must not turn it into an abort, got {reason!r}"
    )
    assert any("vibe check got no answer" in r.getMessage() for r in caplog.records), (
        [r.getMessage() for r in caplog.records]
    )
    assert len(handler.evolutions) == 1
    assert handler.evolutions[0].work == "", (
        "an unanswered vibe check leaves the work field empty — no fallback to "
        "the completion text, and no slowdown state is invented from silence"
    )
    assert handler._slowdown_active is False, "silence is not a slowdown signal"


def test_a_queued_cycle_is_not_ended_by_the_holding_turns_terminal_frame(
    tmp_path, monkeypatch, caplog,
):
    """The measured defect: a queued cycle counted as a completed evolution.

    Measured 2026-10-02 on `main` before this change: with the request in the
    session's pending queue, the frames that follow are the **holding** turn's —
    every one names its request, and they are session broadcasts — and the first
    terminal frame among them ended this cycle as `done`, appending an
    EvolutionLog for a cycle whose own turn never began.

    The frames are the ones the daemon really emits, in its order
    (`daemon.py`): `task_queued`, the holding turn's stream, its `done`, the
    turn's `turn_end`, and `queued_requeue` — which is the frame a queued cycle
    is *guaranteed* to receive, because it is sent after the turn's cwd filter
    is cleared.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
        # the holding turn reporting itself — none of it is this cycle's
        {"request_id": "host-1", "content": "thinking", "delta": True,
         "session_id": "s"},
        {"type": "tool_start", "request_id": "host-1", "tool_name": "bash",
         "arguments": {"command": "true"}},
        {"type": "tool_end", "request_id": "host-1", "tool_name": "bash",
         "content": "ok", "error": False},
        {"request_id": "host-1", "content": "all done", "done": True,
         "delta": False, "session_id": "s"},
        {"type": "turn_end", "session_id": "s"},
        # …and the daemon saying this request is still pending
        {"type": "queued_requeue", "session_id": "s", "request_ids": ["self"]},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED, (
        "this cycle's request never became its own turn, so the holding turn's "
        f"terminal frame is not this cycle's ending — got {reason!r}"
    )
    assert handler.evolutions == [], (
        "a cycle whose own turn never began was counted as an evolution: "
        f"{[getattr(e, 'impact', None) for e in handler.evolutions]}"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("tools=0" in m for m in messages), (
        "the holding turn's tool frames are not this cycle's tools: " + str(messages)
    )
    assert not any("complete (tools=" in m for m in messages), (
        f"nothing here completed: {messages}"
    )


def test_a_requeue_naming_another_request_does_not_answer_for_this_one(
    tmp_path, monkeypatch, caplog,
):
    """The rule reads the id, not the frame type.

    `queued_requeue` carries the ids still pending; only this cycle's own id in
    that list says anything about this cycle's request. A requeue naming somebody
    else is therefore *no answer at all* for this one, and the cycle stays where
    the daemon's own `task_queued` put it — waiting, with the bound as its only
    way out. The alternative was measured and is worse: clearing the flag on a
    frame that names another request sent a still-queued cycle into `_STALLED`,
    whose log line claims its turn stopped reporting.
    """
    import logging

    from emrg.server import scheduler as mod

    assert mod._requeue_names_this_request(
        {"type": "queued_requeue", "request_ids": ["me"]}, "me") is True
    assert mod._requeue_names_this_request(
        {"type": "queued_requeue", "request_ids": ["other"]}, "me") is False
    assert mod._requeue_names_this_request(
        {"type": "queued_requeue"}, "me") is False
    assert mod._requeue_names_this_request(
        {"type": "queued_cancelled", "request_ids": ["me"]}, "me") is False, (
        "only the requeue frame is this rule's business"
    )

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
        # somebody else's request is requeued; ours is not in the list
        {"type": "queued_requeue", "session_id": "s", "request_ids": ["host-1"]},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED, (
        "a requeue that does not name this request answers nothing about it, so "
        f"the cycle is still where the daemon put it — got {reason!r}"
    )
    assert not any("stopped reporting" in r.getMessage() for r in caplog.records), (
        "and nothing here is a wedge: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_the_queue_predicate_answers_only_for_this_requests_queue():
    """The predicate, over every shape the daemon puts on the wire.

    A frame with no `request_id` says "not stated", not "somebody else's": the
    holding turn's `turn_end` and the turn-wrapper's error frame carry none, and
    the loop's existing tests write frames without one — so the question is not
    "does this frame name another request" but "does it answer for *this* one",
    and an id-less frame answers for nobody.
    """
    from emrg.server import scheduler as mod

    mine = mod._about_this_requests_queue
    assert mine({"type": "task_queued", "request_id": "me"}, "me") is True
    assert mine({"type": "task_queued", "request_id": "host-1"}, "me") is False, (
        "another request's queue position is not this request's"
    )
    assert mine({"type": "steer_committed", "request_id": "me"}, "me") is True
    assert mine({"type": "queued_requeue", "request_ids": ["me"]}, "me") is True
    assert mine({"type": "queued_requeue", "request_ids": ["host-1"]}, "me") is False
    assert mine({"type": "queued_requeue"}, "me") is False
    assert mine({"type": "queued_cancelled", "session_id": "s"}, "me") is False, (
        "the drop is terminal but carries no ids; it is read as this request's "
        "only where the flag already says the request was in that queue"
    )
    assert mine({"type": "turn_end", "session_id": "s"}, "me") is False
    assert mine({"error": "Turn ended without reporting: CancelledError"}, "me") is False
    assert mine({"request_id": "me", "content": "hi", "delta": True}, "me") is True, (
        "this cycle's own turn finally reporting is the thing the flag waits for"
    )
    assert mine({"request_id": "host-1", "done": True}, "me") is False
    assert mod._queue_was_dropped({"type": "queued_cancelled"}) is True
    assert mod._queue_was_dropped({"type": "queued_requeue", "request_ids": ["me"]}) is False


def test_a_cycle_that_ran_its_own_turn_still_counts(tmp_path, monkeypatch, caplog):
    """The control: the ordinary path must be untouched by the id rule.

    Without this arm, a body that never recognised its own frames — or one that
    treated every `done` as foreign — would satisfy the test above and leave
    every real cycle hanging until the silence bound.
    """
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "self", "content": "thinking", "delta": True,
         "session_id": "s"},
        {"type": "tool_start", "request_id": "self", "tool_name": "bash",
         "arguments": {"command": "true"}},
        {"type": "tool_end", "request_id": "self", "tool_name": "bash",
         "content": "ok", "error": False},
        {"request_id": "self", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])

    reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._CLEAN_END, (
        f"this cycle's own terminal frame must still end it — got {reason!r}"
    )
    assert len(handler.evolutions) == 1, "it ran, so it counts"
    assert captured["log"].tool_count == 2, (
        "and its own tool frames are counted (the counter advances once per frame "
        "carrying `tool_name` — `tool_start` and `tool_end` both carry it, so one "
        f"call is two): {captured['log'].tool_count}"
    )


def test_another_requests_queue_frame_does_not_relabel_this_cycles_silence(
    tmp_path, monkeypatch, caplog,
):
    """The frames are a session broadcast; `request_id` is what says whose.

    `daemon._broadcast` targets **every** subscriber of the session ("including
    the originator"), so the session's other requests — the host's own message,
    another client's, another task's — land on this socket too. Read as this
    cycle's own, the first such frame turns the stall watchdog off: our turn had
    begun (it emitted frames), the host then typed something into the busy
    session, and our turn then stopped reporting. That silence is the wedge
    `_STALLED` exists to name, and relabelling it `queued` files a real stall as
    "a busy session, nothing to see" — the exact misreading this ending was
    added to prevent, mirrored.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        # this cycle's own turn is running
        {"type": "tool_start", "tool_name": "bash", "arguments": {"command": "true"}},
        {"type": "tool_end", "tool_name": "bash", "content": "ok", "error": False},
        # the host's message goes into the pending queue while our turn runs
        {"type": "task_queued", "request_id": "host-1", "session_id": "s",
         "position": 1},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._STALLED, (
        "a frame naming another request says nothing about this cycle's turn, so "
        f"the silence after our own frames is still a stall — got {reason!r}"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("stalled: no frame for" in m for m in messages), messages
    assert not any("queued behind a busy session" in m for m in messages), (
        f"and nothing may claim the daemon held *this* request: {messages}"
    )


def test_a_foreign_queue_frame_never_becomes_the_position_reported(
    tmp_path, monkeypatch, caplog,
):
    """The position in the message must be this cycle's, not the neighbour's.

    Same broadcast, read as ours: the message would name a queue position that
    belongs to somebody else's request as the evidence for what happened to
    this one — a reading that is precise, checkable, and about the wrong thing.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 3},
        # …and *after* it, the neighbour's, which is the ordering that matters:
        # a flag holding the last position it saw would report the neighbour's
        {"type": "task_queued", "request_id": "host-1", "session_id": "s",
         "position": 7},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED
    messages = [r.getMessage() for r in caplog.records]
    assert any("position 3" in m for m in messages), messages
    assert not any("position 7" in m for m in messages), (
        f"the neighbour's queue position is not evidence about this request: {messages}"
    )


def test_a_queued_cycles_own_silence_is_not_a_stall_when_the_holder_ends(
    tmp_path, monkeypatch, caplog,
):
    """The holding turn's `turn_end` says nothing about this request.

    `turn_end` carries no `request_id` (`daemon.py` broadcasts it as the turn's
    last lifecycle frame), so a rule that only asked "is this somebody else's
    frame" let it through and it cleared the flag — and the silence after it was
    then read as a turn that stopped reporting. Measured on `13329a23`: this
    sequence ends `stalled`, whose log line is *"the turn stopped reporting
    without closing the socket"*, for a cycle that never had a turn here.

    The frames are two shapes the daemon really emits: the bare `turn_end`, and
    the holding turn's frames before it.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 4},
        {"request_id": "host-1", "content": "thinking", "delta": True,
         "session_id": "s"},
        {"type": "turn_end", "session_id": "s"},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED, (
        "the holding turn ending is not this cycle's turn ending, and the "
        f"daemon is still holding its request — got {reason!r}"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert not any("stopped reporting" in m for m in messages), (
        f"nothing here stopped reporting: {messages}"
    )
    assert handler.evolutions == [], "nothing ran, so nothing is counted"


def test_a_dropped_queue_is_its_own_ending(tmp_path, monkeypatch, caplog):
    """The one frame that *is* about this request while it waits: the drop.

    `queued_cancelled` is broadcast when a turn ends by cancel, error or
    disconnect with messages still pending, and the pending list is cleared —
    so a request that was in that queue will never be started. Measured
    2026-10-02 on `13329a23`: this sequence reported `stalled`, the one ending
    whose meaning is a wedge to investigate.

    Both halves are asserted: the ending names the drop, and it is not counted
    (nothing ran). A fix that reached for `queued` here would be wrong in the
    other direction — that reason says the daemon is *still* holding it.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 2},
        {"request_id": "host-1", "content": "thinking", "delta": True,
         "session_id": "s"},
        {"type": "queued_cancelled", "session_id": "s"},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    # The literal rather than the attribute: on a tree without this ending the
    # attribute does not exist, so the red would be an AttributeError instead of
    # the behaviour (this is the convention `test_a_terminal_frame_still_ends_cleanly`
    # uses for `done`).
    assert reason == "queue-dropped", (
        f"the daemon dropped the queue this request was in — got {reason!r}"
    )
    assert handler.evolutions == [], "nothing ran, so nothing is counted"
    messages = [r.getMessage() for r in caplog.records]
    assert any("dropped from the session's queue" in m for m in messages), messages
    assert not any("stopped reporting" in m for m in messages), (
        f"a dropped request is not a wedge: {messages}"
    )
    assert any("nothing ran and nothing is wedged" in m for m in messages), (
        f"and the marker must say so for whoever reads it: {messages}"
    )


def test_the_holding_turns_error_is_not_this_cycles_error(tmp_path, monkeypatch, caplog):
    """The wrapper's error frame is id-less, and it is the *holding* turn's.

    `daemon.py` emits `{"error": "Turn ended without reporting: …"}` from the
    turn wrapper with no `request_id`, before the turn's cwd filter is cleared —
    so a queued cycle receives it too. Read as this cycle's error it becomes
    `server-error`, which is a definite claim that *this* request failed.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
        {"error": "Turn ended without reporting: CancelledError: "},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason != "server-error", (
        "the holding turn's failure is not this cycle's failure — a queued "
        f"request has no turn here to fail — got {reason!r}"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert not any("server error" in m for m in messages), (
        f"and it must not be reported as one: {messages}"
    )


def test_a_holding_turns_done_before_the_answer_is_not_this_cycles_done(
    tmp_path, monkeypatch, caplog,
):
    """The window between our `send` and the daemon's answer is not a safe one.

    `_broadcast` reaches every subscriber of the session, the originator
    included, and the daemon's answer to this cycle travels the same socket as
    the holding turn's frames — so a frame of the *holding* turn can arrive
    before `task_queued` does, at which point this loop does not yet know it is
    queued. Measured 2026-10-02 on `1269ed9b`: the holding turn's `done` there
    ended the cycle as `done`, with an EvolutionLog appended — a cycle counted as
    a completed evolution whose own turn never began, which is the defect #1815
    fixed one round later in the same sequence.

    The frames are the daemon's own shapes: its `delta` and `done` both carry the
    request they belong to (`daemon.py`).
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"request_id": "host-1", "content": "thinking", "delta": True,
         "session_id": "s"},
        {"request_id": "host-1", "content": "All done", "done": True,
         "session_id": "s"},
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED, (
        "the holding turn's completion is not this cycle's completion — this "
        f"cycle never ran — got {reason!r}"
    )
    assert handler.evolutions == [], (
        "and it must not be counted as evolution: "
        f"{[e.get('cycle') if isinstance(e, dict) else e for e in handler.evolutions]}"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert not any("complete (tools=" in m for m in messages), (
        f"nothing here was completed: {messages}"
    )


def test_a_holding_turns_tools_before_the_answer_are_not_this_cycles_progress(
    tmp_path, monkeypatch, caplog,
):
    """The same window, one frame smaller: the holder's tools are not our tools.

    `tool_count` is the cycle's progress measure and is mirrored into the
    heartbeat file, so counting the holding turn's calls writes somebody else's
    activity into this cycle's persisted state. Measured 2026-10-02 on
    `1269ed9b`: the pair below set `tool_count` to 2 for a cycle that ran no tool
    at all.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "tool_start", "request_id": "host-1", "tool_name": "bash",
         "arguments": {"command": "true"}, "session_id": "s"},
        {"type": "tool_end", "request_id": "host-1", "tool_name": "bash",
         "content": "ok", "error": False, "session_id": "s"},
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED, f"got {reason!r}"
    assert handler._cycle_progress.get("tool_count") in (None, 0), (
        "the holding turn's two tool calls became this cycle's progress: "
        f"{handler._cycle_progress}"
    )


def test_a_holding_turns_error_before_the_answer_is_not_this_cycles_error(
    tmp_path, monkeypatch, caplog,
):
    """And the rule holds for the frame that follows it: the wrapper's error.

    `daemon.py` names the request on this frame too (the wrapper's fallback and
    the loop's own LLM-error exit both carry `request_id`, so a client cannot tell
    which spoke), which is what lets a cycle that has not been answered yet
    ignore the *holding* turn's failure. Without the name, this sequence reports
    `server-error` for this cycle — the holder's trouble filed as this request's.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"request_id": "host-1",
         "error": "Turn ended without reporting: CancelledError: "},
        {"type": "task_queued", "request_id": "self", "session_id": "s",
         "position": 1},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == handler._QUEUED, (
        "the holding turn's failure is not this cycle's — this cycle has no turn "
        f"here — got {reason!r}"
    )
    assert not any("server error" in r.getMessage() for r in caplog.records)


def test_this_cycles_own_error_is_still_its_own(tmp_path, monkeypatch, caplog):
    """The other direction: the guard must not swallow *our* failure.

    A rule that ignored every frame naming a request would make this cycle blind
    to its own error frame — the failure would be reported nowhere and the cycle
    would end on the silence bound instead, which is a different lie.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"request_id": "self", "error": "LLM error: boom. Check config..."},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == "server-error", (
        f"our own error frame is still ours — got {reason!r}"
    )
    assert any("server error" in r.getMessage() for r in caplog.records)


def test_a_frame_naming_another_request_is_never_this_cycles():
    """The predicate, over the shapes the daemon puts on the wire.

    Not the same question as `_about_this_requests_queue`: this one asks
    "is this somebody else's frame", which is a fact at any moment — including
    before the daemon has answered this cycle at all.
    """
    from emrg.server import scheduler as mod

    theirs = mod._names_another_request
    assert theirs({"request_id": "host-1"}, "me") is True
    assert theirs({"type": "tool_end", "request_id": "host-1"}, "me") is True
    assert theirs({"request_id": "me"}, "me") is False
    assert theirs({"type": "task_queued", "request_id": "me"}, "me") is False
    assert theirs({"type": "queued_requeue", "request_ids": ["host-1"]}, "me") is True
    assert theirs({"type": "queued_requeue", "request_ids": ["me"]}, "me") is False
    assert theirs({"type": "queued_requeue", "request_ids": ["host-1", "me"]}, "me") is False, (
        "a requeue naming this request as well is still partly about it"
    )
    assert theirs({"type": "turn_end", "session_id": "s"}, "me") is False, (
        "an id-less frame names nobody; the queued guard is what reads those"
    )
    assert theirs({"type": "queued_cancelled", "session_id": "s"}, "me") is False
    assert theirs({"request_id": ""}, "me") is False, (
        "an empty id is not a claim about somebody else"
    )


def test_another_clients_failed_compact_is_not_this_cycles_error(
    tmp_path, monkeypatch, caplog,
):
    """A session-level command's failure is not this cycle's failure.

    `compact` is handled with no busy check (`daemon.py::_process_message`) and
    its result is **broadcast to every subscriber** of the session
    (`_handle_compact`), the failure branch included. So while this cycle runs
    its own turn, another client's failed compact arrives here — and it carries
    no request id, only the session's `type`. Read as this cycle's error it
    aborts a running cycle and files that client's command as this request's
    failure. Measured 2026-10-02: the log line read "server error: Compact
    failed: …" for a cycle whose own turn was one delta old.

    The frames are the daemon's own shape: a `delta` naming this request (so the
    turn is this cycle's), then the neighbour's broadcast.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"request_id": "self", "content": "working", "delta": True,
         "session_id": "s"},
        {"type": "compact_result", "session_id": "s", "messages_compacted": 0,
         "error": "Compact failed: provider refused"},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    messages = [r.getMessage() for r in caplog.records]
    assert reason != "server-error", (
        "another client's compact failed, not this cycle's turn — got "
        f"{reason!r} from {messages}"
    )
    assert not any("server error" in m for m in messages), (
        f"and it must not be reported as this cycle's failure: {messages}"
    )
    assert handler.evolutions == [], "nothing ran, so nothing is counted"


def test_the_daemons_direct_reply_to_our_own_command_is_still_our_error(
    tmp_path, monkeypatch, caplog,
):
    """The discriminating direction: our own rejection must still abort.

    The daemon answers a command **this connection** sent directly, and those
    replies carry no `type` and no request id (`daemon.py`: `{"error": "invalid
    task: …"}`, `{"error": "message must be a JSON object"}`). A rule that asked
    only "does this name this request" would drop this one on the floor, and the
    cycle would end on the silence bound instead — reporting a stall for a
    request the daemon refused outright.
    """
    import logging

    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"error": "invalid task: boom"},
    ])

    with caplog.at_level(logging.WARNING):
        reason = asyncio.run(handler._run_evolution_cycle())

    assert reason == "server-error", f"got {reason!r}"
    assert any("server error: invalid task" in r.getMessage()
               for r in caplog.records)


def test_an_error_frame_belongs_to_the_request_it_names():
    """The predicate, over the three shapes that reach this socket.

    A named error is the named request's; an unnamed one is this cycle's only
    when it is a direct reply, which is the shape with no `type` at all.
    """
    from emrg.server import scheduler as mod

    mine = mod._error_is_this_requests
    assert mine({"request_id": "me", "error": "boom"}, "me") is True
    assert mine({"type": "tool_end", "request_id": "me", "error": True}, "me") is True
    assert mine({"request_id": "host-1", "error": "boom"}, "me") is False, (
        "the name settles it: that failure belongs to the request it names"
    )
    assert mine({"error": "invalid task: boom"}, "me") is True, (
        "the daemon's direct reply to our own command carries no type"
    )
    assert mine({"error": "unknown message type", "received": "x"}, "me") is True
    assert mine({"type": "compact_result", "error": "Compact failed: x"}, "me") is False
    assert mine({"type": "messages_compacted", "error": "x"}, "me") is False
    assert mine({"request_id": "", "error": "boom"}, "me") is True, (
        "an empty id is 'not stated', so the frame is read by its shape — the "
        "same reading `_names_another_request` gives it"
    )


# ── the liveness clock belongs to this cycle (measured 2026-10-02) ──────────
#
# The three tests below are one mechanism read in both directions: the silence
# bound must fire for a frame that is not this cycle's, and must *not* fire for
# one that is. The middle one is the defect (a queued cycle waiting for as long
# as the holding turn talks); the third is the over-fix it invites (a live turn
# killed for streaming).


def _run_spaced(tmp_path, monkeypatch, caplog, frames, n_spaced, bound, spacing,
                level=logging.WARNING):
    """Run a cycle whose `n_spaced` frames each arrive `spacing` after the last.

    Returns (reason, consumed, elapsed): `consumed` is how many frames the loop
    took in, which is the reading that distinguishes "the bound fired" from "the
    neighbour's stream kept it alive".
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", bound)
    delays = {i: spacing for i in range(len(frames) - n_spaced, len(frames))}
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=frames, delays=delays)
    import time as _time

    started = _time.monotonic()
    with caplog.at_level(level):
        reason = asyncio.run(handler._run_evolution_cycle())
    return reason, handler._silence_ws.consumed, _time.monotonic() - started


def test_a_holding_turns_stream_does_not_extend_a_queued_cycles_bound(
    tmp_path, monkeypatch, caplog,
):
    """The bound is a queued cycle's only way out, so it must not be extendable.

    `_QUEUED` is reached on a timeout, and the holding turn reports itself on this
    socket the whole time it runs — so with the liveness reset before the guards,
    every one of its frames pushed the deadline back. Measured 2026-10-02 on
    `31834cc9`: with a 1s bound and six foreign frames spaced 0.5s apart, the
    cycle took **4.03s** to end (it consumed the whole stream) instead of the ~1s
    the bound promises, and it held `_cycle_running` — and the task's next run —
    for as long as the neighbour kept talking. Requirement 5 is exactly that no
    handler holds the slot on an open socket.

    The numbers here are shrunken so the test is fast; what it asserts is the
    shape: the cycle gives up at *its own* bound, not at the end of somebody
    else's stream.
    """
    from emrg.server import scheduler as mod

    frames = [{"type": "task_queued", "request_id": "self", "session_id": "s",
               "position": 1}]
    for _ in range(6):
        frames.append({"request_id": "host-1", "content": "…", "delta": True,
                       "session_id": "s"})
    reason, consumed, elapsed = _run_spaced(
        tmp_path, monkeypatch, caplog, frames, n_spaced=6,
        bound=0.3, spacing=0.15,
    )

    assert reason == mod.TaskHandler._QUEUED, f"got {reason!r}"
    assert consumed <= 3, (
        "the cycle walked into the holding turn's stream instead of ending at "
        f"its own bound: it took in {consumed} of 7 frames"
    )
    assert elapsed < 0.6, (
        f"the bound is 0.3s and the cycle waited {elapsed:.2f}s — a frame that "
        "is not this cycle's extended its liveness clock"
    )


def test_a_holding_turns_stream_does_not_keep_a_wedged_turn_alive(
    tmp_path, monkeypatch, caplog,
):
    """The same clock, without the queue: this cycle's turn went quiet.

    A neighbour's traffic in the session — another client's failed `compact`,
    say — is not evidence that *this* cycle's turn is alive, so a turn that
    stopped reporting must still be reported as `stalled` at its own bound. With
    the reset before the guards the neighbour's stream is what the watchdog
    measured, and the wedge it exists to find is the thing it never sees.
    """
    from emrg.server import scheduler as mod

    frames = [{"request_id": "self", "content": "…", "delta": True,
               "session_id": "s"}]
    for _ in range(6):
        frames.append({"request_id": "host-1", "content": "…", "delta": True,
                       "session_id": "s"})
    reason, consumed, elapsed = _run_spaced(
        tmp_path, monkeypatch, caplog, frames, n_spaced=6,
        bound=0.3, spacing=0.15, level=logging.ERROR,
    )

    assert reason == mod.TaskHandler._STALLED, f"got {reason!r}"
    assert consumed <= 3, f"the wedged turn waited through a foreign stream: {consumed}"
    assert elapsed < 0.6, f"the watchdog fired after {elapsed:.2f}s, not at 0.3s"


def test_this_cycles_own_stream_still_extends_it(tmp_path, monkeypatch, caplog):
    """The discriminating direction: our own frames must keep the cycle alive.

    A long turn streams `delta` frames for minutes, and the watchdog exists for
    the case where they *stop*. A fix that measured wall-clock time, or that
    reset the clock only on terminal frames, would kill every healthy long turn
    as `stalled` — so the frames here are this cycle's own, and the cycle must
    take every one of them before giving up.
    """
    from emrg.server import scheduler as mod

    frames = [{"request_id": "self", "content": "…", "delta": True,
               "session_id": "s"} for _ in range(5)]
    reason, consumed, elapsed = _run_spaced(
        tmp_path, monkeypatch, caplog, frames, n_spaced=5,
        bound=0.2, spacing=0.1, level=logging.ERROR,
    )

    assert consumed == 5, (
        "this cycle's own frames stopped counting as liveness: it took in "
        f"{consumed} of 5"
    )
    assert elapsed > 0.5, (
        f"the cycle gave up after {elapsed:.2f}s while its own turn was still "
        "streaming — the bound must measure this cycle's silence"
    )
    assert reason == mod.TaskHandler._STALLED, f"got {reason!r}"


def test_a_foreign_frame_does_not_cancel_this_cycles_tool_bound(
    tmp_path, monkeypatch, caplog,
):
    """The tool bound survives a neighbour's frame, for the same reason.

    `tool_deadline` is the *longer* of the two clocks while a tool runs — a call
    that declared how long it needs. The reset that clears it belongs with the
    liveness reset, and both belong after the guards: cleared by a foreign frame,
    the deadline collapses back to the round bound, and a cycle whose tool is
    legitimately still running is torn down as `stalled` with the tool call
    abandoned (measured shape 2026-10-02; the placement is the fix).
    """
    from emrg.server import scheduler as mod

    monkeypatch.setattr(mod, "_ROUND_SILENCE_SECONDS", 0.2)
    monkeypatch.setattr(mod, "_TOOL_SILENCE_GRACE_SECONDS", 0.05)
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    _silence_frames(handler, tmp_path, monkeypatch, frames=[
        {"type": "tool_start", "request_id": "self", "tool_name": "bash",
         "arguments": {"command": "sleep 1", "timeout": 1.2}, "session_id": "s"},
        {"request_id": "host-1", "content": "…", "delta": True, "session_id": "s"},
    ], delays={1: 0.3})
    import time as _time

    started = _time.monotonic()
    with caplog.at_level(logging.ERROR):
        reason = asyncio.run(handler._run_evolution_cycle())
    elapsed = _time.monotonic() - started

    assert reason == mod.TaskHandler._STALLED, f"got {reason!r}"
    assert elapsed > 0.9, (
        f"the cycle gave up after {elapsed:.2f}s: the declared 1.2s tool bound was "
        "replaced by the round bound when somebody else's frame arrived"
    )
