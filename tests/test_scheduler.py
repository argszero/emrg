"""Unit tests for emrg.server.scheduler — task loading, migration, and lifecycle."""

from __future__ import annotations

import asyncio
import subprocess
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
    """Tasks with unknown handler type are skipped; self-heal still adds emrg-task."""
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

    assert len(coros) == 1  # the self-healed emrg-task
    assert sched._handlers[0].name == "emrg-task"
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
    """Promote tasks start an EvolutionHandler with the promote template."""
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


def test_task_scheduler_total_evolutions():
    """total_evolutions sums per-handler evolution log counts."""
    from emrg.protocol import EvolutionLog

    sched = TaskScheduler(InstanceIdentity())
    h1 = EvolutionHandler(name="a", config={}, interval=60, identity=InstanceIdentity())
    h2 = EvolutionHandler(name="b", config={}, interval=60, identity=InstanceIdentity())
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


# ── Evolution workspace self-heal (rant 2026-08-06T20:42:05, 方案 C) ──────


def _make_handler(tmp_path, name="emrg-task", project="emrg", path=None):
    """Build an EvolutionHandler pointed at a tmp config dir."""
    from emrg.server import scheduler as mod
    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    handler = EvolutionHandler(
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


class FakeGitRun:
    """Controllable subprocess.run fake for git commands."""

    def __init__(self, git_repo=True, tags="v0.2.7", clone_fails=False, remote_head="abc123",
                 origin_url="", ls_remote_stderr="", clone_stderr="", clone_fail_once=False):
        self.calls = []
        self.git_repo = git_repo
        self.tags = tags
        self.clone_fails = clone_fails
        self.remote_head = remote_head
        self.origin_url = origin_url
        self.ls_remote_stderr = ls_remote_stderr
        self.clone_stderr = clone_stderr
        self.clone_fail_once = clone_fail_once
        self._clone_calls = 0

    @staticmethod
    def _norm(cmd):
        """Strip `git -c key=value` config pairs (http.connectTimeout=…)."""
        out, i = [], 1
        args = list(cmd)
        while i < len(args):
            if args[i] == "-c" and i + 1 < len(args):
                i += 2
                continue
            out.append(args[i])
            i += 1
        return out

    def __call__(self, cmd, *args, **kwargs):
        self.calls.append((list(cmd), kwargs.get("cwd")))
        cwd = kwargs.get("cwd") or ""
        if cmd[0] == "git":
            sub = self._norm(cmd)
            if sub and sub[0] == "rev-parse":
                if "--is-inside-work-tree" in sub:
                    return _R(0, "true\n" if self.git_repo else "false\n")
                if "HEAD" in sub:
                    return _R(0, "abc123\n")
            if sub and sub[0] == "remote":
                if sub[1] == "get-url":
                    return _R(0, self.origin_url + "\n")
                if sub[1] == "set-url":
                    return _R(0, "")
            if sub and sub[0] == "ls-remote":
                # `git ls-remote origin master` → "<sha>\trefs/heads/master".
                # When ls_remote_stderr is set, only the https-origin form
                # fails — the SSH retry (git@github.com:…) succeeds.
                # NB: list `in` is element-equality — use substring scan.
                ssh_retry = any("git@github.com" in str(c) for c in cmd)
                if self.ls_remote_stderr and not ssh_retry:
                    return _R(128, "", self.ls_remote_stderr)
                return _R(0, f"{self.remote_head}\trefs/heads/master\n")
            if sub and sub[0] == "clone":
                self._clone_calls += 1
                if self.clone_fails and (not self.clone_fail_once or self._clone_calls == 1):
                    raise _CalledProcessErrorStub(self.clone_stderr or "clone failed",
                                                  stderr=self.clone_stderr)
                target = Path(cmd[-1])
                target.mkdir(parents=True, exist_ok=True)
                return _R(0, "")
            if sub and sub[0] == "tag":
                return _R(0, self.tags + "\n")
            if sub and sub[0] == "checkout":
                return _R(0, "")
            if sub and sub[0] == "config":
                return _R(0, "")  # getter → empty → setter will run
        return _R(0, "")


class _R:
    def __init__(self, returncode, stdout, stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _CalledProcessErrorStub(subprocess.CalledProcessError):
    def __init__(self, msg, stderr=""):
        super().__init__(returncode=1, cmd=["git", "clone"], output=msg, stderr=stderr)


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

    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text(yaml.safe_dump([
        {"name": "emrg", "path": "/dev/machine/custom/emrg",
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
    assert data[0]["path"] == "/dev/machine/custom/emrg"  # untouched


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


def test_ensure_evolution_workspace_dev_repo_untouched(tmp_path):
    """A real writable git repo (dev machine) is used as-is — no clone."""
    import subprocess as real_subprocess

    from emrg.server import scheduler as mod

    repo = tmp_path / "dev-emrg"
    repo.mkdir()
    real_subprocess.run(["git", "init", "-q", str(repo)], check=True)
    real_subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    real_subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    real_subprocess.run(
        ["git", "-C", str(repo), "add", "."], check=True)
    real_subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"], check=True)

    orig_config = mod.config_dir
    mod.config_dir = lambda: tmp_path
    try:
        handler = EvolutionHandler(
            name="emrg-task",
            config={"project": "emrg"},
            interval=60,
            identity=InstanceIdentity(),
        )
    finally:
        mod.config_dir = orig_config
    handler._source_dir = str(repo)
    handler.project_path = str(repo)

    fake = FakeGitRun()
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        ok = handler._ensure_evolution_workspace()
    finally:
        mod.subprocess.run = orig_run

    assert ok is True
    assert handler._source_dir == str(repo)  # unchanged
    assert not any("clone" in c[0] for c in fake.calls), f"unexpected clone: {fake.calls}"


def test_ensure_evolution_workspace_clones_and_aligns(tmp_path):
    """Non-git source_dir → clone into evolution workspace + align + projects.yml self-heal."""
    from emrg.server import scheduler as mod

    evolve_dir = tmp_path / "evolution" / "emrg"
    mod.EVOLUTION_CWD = tmp_path / "evolution"

    projects_yml = tmp_path / "projects.yml"
    projects_yml.write_text(yaml.safe_dump([]))

    handler = _make_handler(tmp_path, path=str(tmp_path / "install" / "source" / "emrg"))

    # Installed version hint → tag alignment. The code reads
    # Path.home()/.emrg/install/version.txt — patch home so the test is
    # hermetic (CI hosts don't have ~/.emrg/install).
    import pathlib as _pathlib
    install_dir = tmp_path / ".emrg" / "install"
    install_dir.mkdir(parents=True)
    (install_dir / "version.txt").write_text("0.2.7", encoding="utf-8")

    fake = FakeGitRun(git_repo=False, tags="v0.2.7")
    orig_run = mod.subprocess.run
    orig_evolve = mod.EVOLUTION_CWD
    orig_config = mod.config_dir
    orig_home = _pathlib.Path.home
    mod.subprocess.run = fake
    mod.config_dir = lambda: tmp_path
    _pathlib.Path.home = classmethod(lambda cls: tmp_path)
    try:
        ok = handler._ensure_evolution_workspace()
    finally:
        mod.subprocess.run = orig_run
        mod.config_dir = orig_config
        mod.EVOLUTION_CWD = orig_evolve
        _pathlib.Path.home = orig_home

    assert ok is True
    assert handler._source_dir == str(evolve_dir)
    # clone called with repo URL + target
    clone_calls = [c for c in fake.calls if "clone" in c[0]]
    assert len(clone_calls) == 1
    # tag alignment: checkout -B master v0.2.7
    checkout_calls = [c for c in fake.calls if c[0][1] == "checkout"]
    assert any("v0.2.7" in c[0] for c in checkout_calls), f"no tag checkout: {checkout_calls}"
    # git identity configured
    config_calls = [c for c in fake.calls if c[0][1] == "config"]
    assert any("user.name" in c[0] for c in config_calls)
    assert any("user.email" in c[0] for c in config_calls)
    # projects.yml self-heal
    data = yaml.safe_load(projects_yml.read_text(encoding="utf-8"))
    assert any(e.get("name") == "emrg" and e.get("path") == str(evolve_dir) for e in data)


def test_ensure_evolution_workspace_clone_failure_skips(tmp_path):
    """Clone failure (no network) → returns False so the cycle is skipped."""
    from emrg.server import scheduler as mod

    mod.EVOLUTION_CWD = tmp_path / "evolution"
    handler = _make_handler(tmp_path, path=str(tmp_path / "nonexistent"))
    handler._repo_url = "https://github.com/argszero/emrg.git"

    fake = FakeGitRun(git_repo=False, clone_fails=True)
    orig_run = mod.subprocess.run
    orig_evolve = mod.EVOLUTION_CWD
    mod.subprocess.run = fake
    try:
        ok = handler._ensure_evolution_workspace()
    finally:
        mod.subprocess.run = orig_run
        mod.EVOLUTION_CWD = orig_evolve

    assert ok is False
    assert handler._source_dir != str(mod.EVOLUTION_CWD / "emrg")


# ── HTTPS→SSH fallback for blocked github.com:443 (2026-08-08) ─────
# Some networks block github.com:443 while SSH port 22 stays open — the
# self-heal clone and the saturation auto-resume (ls-remote) must not
# hard-depend on https reaching github.com.

def test_ensure_origin_reachable_switches_to_ssh_when_https_blocked(tmp_path):
    """https origin unreachable (connection error) → origin switched to SSH."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, path=str(tmp_path))
    handler._origin_probed = False
    fake = FakeGitRun(
        origin_url="https://github.com/argszero/emrg.git",
        ls_remote_stderr=(
            "fatal: unable to access 'https://github.com/argszero/emrg.git/': "
            "Failed to connect to github.com port 443 after 4004 ms: "
            "Couldn't connect to server"
        ),
    )
    orig_run = mod.subprocess.run
    orig_origin = mod.git_origin_url
    mod.subprocess.run = fake
    mod.git_origin_url = lambda cwd: "https://github.com/argszero/emrg.git"
    try:
        handler._ensure_origin_reachable()
    finally:
        mod.subprocess.run = orig_run
        mod.git_origin_url = orig_origin

    set_url_calls = [
        c for c in fake.calls
        if c[0][0] == "git" and c[0][1] == "remote" and c[0][2] == "set-url"
    ]
    assert len(set_url_calls) == 1, f"expected one set-url, got {fake.calls}"
    assert set_url_calls[0][0][4] == "git@github.com:argszero/emrg.git"


def test_ensure_origin_reachable_probes_only_once(tmp_path):
    """One-shot probe: a second call never re-runs git."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, path=str(tmp_path))
    handler._origin_probed = False
    fake = FakeGitRun(
        origin_url="https://github.com/argszero/emrg.git",
        ls_remote_stderr="fatal: unable to access: Failed to connect",
    )
    orig_run = mod.subprocess.run
    orig_origin = mod.git_origin_url
    mod.subprocess.run = fake
    mod.git_origin_url = lambda cwd: "https://github.com/argszero/emrg.git"
    try:
        handler._ensure_origin_reachable()
        handler._ensure_origin_reachable()
    finally:
        mod.subprocess.run = orig_run
        mod.git_origin_url = orig_origin

    set_url_calls = [
        c for c in fake.calls
        if c[0][0] == "git" and c[0][1] == "remote" and c[0][2] == "set-url"
    ]
    assert len(set_url_calls) == 1


def test_ensure_origin_reachable_keeps_https_when_reachable(tmp_path):
    """ls-remote succeeds → origin untouched."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, path=str(tmp_path))
    handler._origin_probed = False
    fake = FakeGitRun(origin_url="https://github.com/argszero/emrg.git")
    orig_run = mod.subprocess.run
    orig_origin = mod.git_origin_url
    mod.subprocess.run = fake
    mod.git_origin_url = lambda cwd: "https://github.com/argszero/emrg.git"
    try:
        handler._ensure_origin_reachable()
    finally:
        mod.subprocess.run = orig_run
        mod.git_origin_url = orig_origin

    set_url_calls = [
        c for c in fake.calls
        if c[0][0] == "git" and c[0][1] == "remote" and c[0][2] == "set-url"
    ]
    assert set_url_calls == []


def test_ensure_origin_reachable_ignores_non_connection_errors(tmp_path):
    """Auth/404 failures never switch the origin."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, path=str(tmp_path))
    handler._origin_probed = False
    fake = FakeGitRun(
        origin_url="https://github.com/argszero/emrg.git",
        ls_remote_stderr="remote: Repository not found.",
    )
    orig_run = mod.subprocess.run
    orig_origin = mod.git_origin_url
    mod.subprocess.run = fake
    mod.git_origin_url = lambda cwd: "https://github.com/argszero/emrg.git"
    try:
        handler._ensure_origin_reachable()
    finally:
        mod.subprocess.run = orig_run
        mod.git_origin_url = orig_origin

    set_url_calls = [
        c for c in fake.calls
        if c[0][0] == "git" and c[0][1] == "remote" and c[0][2] == "set-url"
    ]
    assert set_url_calls == []


def test_ensure_evolution_workspace_clone_falls_back_to_ssh(tmp_path):
    """https clone connection failure → retried via SSH, workspace usable."""
    import pathlib as _pathlib

    from emrg.server import scheduler as mod

    evolve_dir = tmp_path / "evolution" / "emrg"
    mod.EVOLUTION_CWD = tmp_path / "evolution"
    handler = _make_handler(tmp_path, path=str(tmp_path / "nonexistent"))
    handler._repo_url = "https://github.com/argszero/emrg.git"

    install_dir = tmp_path / ".emrg" / "install"
    install_dir.mkdir(parents=True)
    (install_dir / "version.txt").write_text("0.2.7", encoding="utf-8")

    fake = FakeGitRun(
        git_repo=False, tags="v0.2.7",
        clone_fails=True, clone_fail_once=True,
        clone_stderr=(
            "fatal: unable to access 'https://github.com/argszero/emrg.git/': "
            "Failed to connect to github.com port 443 after 10013 ms: "
            "Couldn't connect to server"
        ),
    )
    orig_run = mod.subprocess.run
    orig_evolve = mod.EVOLUTION_CWD
    orig_config = mod.config_dir
    orig_home = _pathlib.Path.home
    mod.subprocess.run = fake
    mod.config_dir = lambda: tmp_path
    _pathlib.Path.home = classmethod(lambda cls: tmp_path)
    try:
        ok = handler._ensure_evolution_workspace()
    finally:
        mod.subprocess.run = orig_run
        mod.config_dir = orig_config
        mod.EVOLUTION_CWD = orig_evolve
        _pathlib.Path.home = orig_home

    assert ok is True
    assert handler._source_dir == str(evolve_dir)
    clone_calls = [c for c in fake.calls if "clone" in c[0]]
    assert len(clone_calls) == 2, f"expected https + ssh clone, got {fake.calls}"
    assert clone_calls[0][0][-2] == "https://github.com/argszero/emrg.git"
    assert clone_calls[1][0][-2] == "git@github.com:argszero/emrg.git"


def test_remote_advanced_ssh_fallback_when_https_blocked(tmp_path):
    """ls-remote over a blocked https origin → retried via the SSH URL."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    fake = FakeGitRun(
        origin_url="https://github.com/argszero/emrg.git",
        ls_remote_stderr=(
            "fatal: unable to access 'https://github.com/argszero/emrg.git/': "
            "Failed to connect to github.com port 443"
        ),
        remote_head="9f8e7d6",  # != local abc123 → advanced
    )
    orig_run = mod.subprocess.run
    orig_origin = mod.git_origin_url
    mod.subprocess.run = fake
    mod.git_origin_url = lambda cwd: "https://github.com/argszero/emrg.git"
    try:
        assert handler._remote_advanced() is True
    finally:
        mod.subprocess.run = orig_run
        mod.git_origin_url = orig_origin

    ls_calls = [c for c in fake.calls if "ls-remote" in c[0]]
    assert len(ls_calls) == 2, f"expected https + ssh ls-remote, got {fake.calls}"
    assert "git@github.com:argszero/emrg.git" in ls_calls[1][0]


# ── EvolutionHandler cycle truncation detection ──────────────────
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
    handler._ensure_evolution_workspace = lambda: True
    handler._build_evolution_prompt = lambda: "test prompt"
    handler._get_git_head = lambda: "abc123"  # HEAD unchanged
    captured = {}
    async def _fake_write_log(log):
        captured["log"] = log
    handler._write_evolution_log = _fake_write_log
    mod.connect_to_server = _fake_connect
    return handler, captured


def test_evolution_cycle_truncated_not_empty_not_complete(tmp_path):
    """Truncated done frame → flagged truncated, NOT an empty cycle, impact reflects it."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"tool_name": "bash"},
        {"request_id": "r1", "content": "Exceeded maximum tool call rounds (270).",
         "done": True, "delta": False, "session_id": "s"},
    ])
    asyncio.run(handler._run_evolution_cycle())
    assert handler._empty_cycles == 0, \
        "truncated cycle must not advance the idle-halt backoff"
    impact = captured["log"].impact
    assert any("truncated" in i for i in impact), impact
    assert "truncated=max-tool-rounds" in impact, impact
    assert not any(i.endswith("-complete") for i in impact), impact


def test_evolution_cycle_complete_unchanged_head_still_empty(tmp_path):
    """Normal completion with unchanged HEAD keeps the existing empty-cycle semantics."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])
    asyncio.run(handler._run_evolution_cycle())
    assert handler._empty_cycles == 1, \
        "unchanged-HEAD complete cycle is still counted as empty (existing behavior)"
    impact = captured["log"].impact
    assert any(i.endswith("-complete") for i in impact), impact
    assert "truncated=max-tool-rounds" not in impact, impact


def test_evolution_cycle_aborted_error_not_counted(tmp_path):
    """Server error frame (e.g. 'session busy') → no evolution log, no count."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"error": "session busy"},
    ])
    asyncio.run(handler._run_evolution_cycle())
    assert "log" not in captured, "aborted cycle must not write an evolution log"
    assert handler.evolutions == [], "aborted cycle must not append to evolutions"
    assert handler._empty_cycles == 0, \
        "aborted cycle must not advance the idle-halt backoff (agent never ran)"


def test_evolution_cycle_aborted_resets_empty_streak(tmp_path):
    """Aborted cycle resets a pre-existing empty streak (blocked ≠ NTE)."""
    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"error": "session busy"},
    ])
    handler._empty_cycles = 5
    asyncio.run(handler._run_evolution_cycle())
    assert "log" not in captured
    assert handler._empty_cycles == 0, "abort resets the streak (not a real empty cycle)"


# ── Connect-failure alerting (G129, rant 2026-08-09T08:03:46) ─────
# GUI tests once overwrote the real ~/.emrg/emrgd.port with fake values,
# so the evolution cycle failed to reach the daemon for 10 hours with only
# a WARNING log. Consecutive failures must escalate to ERROR + carry an
# actionable hint (check the port file), never silently swallow.

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
                assert handler._empty_cycles == 0, "connect failure ≠ empty cycle"
        assert handler._connect_failures == handler._CONNECT_FAIL_ALERT
        # 第 3 次（达到阈值）必须出现 ERROR 告警，且提示检查 port 文件
        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_msgs, "expected an ERROR alert after threshold"
        assert any("emrgd.port" in m for m in error_msgs), error_msgs
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


# ── Saturation halt auto-resume on upstream advance ───────────────
# The halt skips scheduled runs entirely, so a halted handler can never
# detect a HEAD change itself (only /trigger could resume it). If every
# instance halted during an idle stretch, new upstream work would go
# unnoticed — the halt must auto-resume when origin/master advances.

def test_saturation_halt_active_true_when_remote_unchanged(tmp_path):
    """At/above threshold + unchanged remote → tick skipped (halt stays)."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._empty_cycles = 30  # == _IDLE_HALT_THRESHOLD
    fake = FakeGitRun(remote_head="abc123")  # == local HEAD → not advanced
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        assert handler._saturation_halt_active() is True
        assert handler._empty_cycles == 30  # counter untouched
    finally:
        mod.subprocess.run = orig_run


def test_saturation_halt_resumes_and_resets_when_remote_advanced(tmp_path):
    """At/above threshold + remote advanced → resume, counter reset to 0."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._empty_cycles = 30
    fake = FakeGitRun(remote_head="9f8e7d6")  # != local abc123 → advanced
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        assert handler._saturation_halt_active() is False
        assert handler._empty_cycles == 0  # reset → scheduled runs resume
    finally:
        mod.subprocess.run = orig_run


def test_saturation_halt_active_false_below_threshold(tmp_path):
    """Below threshold → never halt (remote state irrelevant)."""
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._empty_cycles = 10
    assert handler._saturation_halt_active() is False
    assert handler._empty_cycles == 10


def test_remote_advanced_false_without_git_repo(tmp_path):
    """Not a git repo / ls-remote fails → False (stay halted, no crash)."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    fake = FakeGitRun(git_repo=False)
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        assert handler._remote_advanced() is False
    finally:
        mod.subprocess.run = orig_run
