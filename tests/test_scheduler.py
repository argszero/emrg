"""Unit tests for emrg.server.scheduler — task loading, migration, and lifecycle."""

from __future__ import annotations

import asyncio
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
    """Non-emrg task without any repo config → _ensure_evolution_workspace no-ops."""
    handler = TaskHandler(
        name="docs-task",
        config={"path": "/tmp/plain-folder"},
        interval=1800,
        identity=InstanceIdentity(),
    )
    assert handler._repo_configured is False
    assert handler._ensure_evolution_workspace() is True  # skip, not block


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
        # cmd[0] 可能是字面 "git"（dev 环境）或 resolve_git_gh() 解析出的
        # 绝对路径（bundled git，2026-08-12 workspace-not-ready 事故修复后）——
        # 统一按 basename 判断，避免测试在两种环境下行为不一致。
        # Windows 上 resolve_git_gh() 返回 git.EXE（大写后缀，2026-08-12 v0.2.29
        # Build Release Windows gate 实测）→ 比较必须大小写不敏感。
        cmd_head = Path(cmd[0]).name.lower()
        if cmd_head in ("git", "git.exe"):
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
    fake = FakeGitRun()
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        handler = TaskHandler(
            name="emrg-task",
            config={"project": "emrg"},
            interval=60,
            identity=InstanceIdentity(),
        )
        # config_dir must stay patched through _ensure_evolution_workspace():
        # its clone branch calls _ensure_project_entry(), which writes
        # config_dir()/projects.yml — an unpatched call would pollute the real
        # ~/.emrg/projects.yml (2026-08-12 incident: pytest temp path leaked
        # into real home).
        handler._source_dir = str(repo)
        handler.project_path = str(repo)
        ok = handler._ensure_evolution_workspace()
    finally:
        mod.subprocess.run = orig_run
        mod.config_dir = orig_config

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
    orig_config = mod.config_dir
    mod.subprocess.run = fake
    # config_dir patched through the call: the clone branch would call
    # _ensure_project_entry() and write config_dir()/projects.yml — keep it
    # hermetic so a future fake change can't pollute real ~/.emrg/projects.yml
    # (2026-08-12 pytest-temp-path leak incident).
    mod.config_dir = lambda: tmp_path
    try:
        ok = handler._ensure_evolution_workspace()
    finally:
        mod.subprocess.run = orig_run
        mod.EVOLUTION_CWD = orig_evolve
        mod.config_dir = orig_config

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
        if Path(c[0][0]).name.lower() in ("git", "git.exe") and c[0][1] == "remote" and c[0][2] == "set-url"
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
        if Path(c[0][0]).name.lower() in ("git", "git.exe") and c[0][1] == "remote" and c[0][2] == "set-url"
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
        if Path(c[0][0]).name.lower() in ("git", "git.exe") and c[0][1] == "remote" and c[0][2] == "set-url"
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
        if Path(c[0][0]).name.lower() in ("git", "git.exe") and c[0][1] == "remote" and c[0][2] == "set-url"
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
    assert any(i.startswith("cycle-") for i in impact), \
        f"impact tag uses new cycle- prefix (rant 2026-08-12T18:03:26), got {impact}"
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
# low-frequency full cycles: saturated ticks still run, just at the heartbeat
# interval. Upstream advance auto-resumes (counter reset, normal frequency).

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


def test_saturation_heartbeat_active_true_when_remote_unchanged(tmp_path):
    """At/above threshold + unchanged remote → heartbeat cadence (not skip)."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._empty_cycles = 30  # == _IDLE_HALT_THRESHOLD
    fake = FakeGitRun(remote_head="abc123")  # == local HEAD → not advanced
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        assert handler._saturation_heartbeat_active() is True
        assert handler._empty_cycles == 30  # counter untouched
        assert handler._heartbeat_interval() == 480  # 60s task → 8 min
    finally:
        mod.subprocess.run = orig_run


def test_saturation_heartbeat_log_message_no_skip(tmp_path, caplog):
    """Saturation log must say heartbeat, never 'skipping scheduled run'."""
    import logging
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._empty_cycles = 30
    fake = FakeGitRun(remote_head="abc123")
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        with caplog.at_level(logging.INFO, logger="emrg.server.scheduler"):
            assert handler._saturation_heartbeat_active() is True
        msgs = " ".join(r.message for r in caplog.records)
        assert "skipping scheduled run" not in msgs, \
            "old complete-halt log must not appear (rant 09:35:55)"
        assert "heartbeat" in msgs and "never halting" in msgs, msgs
    finally:
        mod.subprocess.run = orig_run


def test_saturation_heartbeat_resumes_and_resets_when_remote_advanced(tmp_path):
    """At/above threshold + remote advanced → normal frequency, counter reset."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._empty_cycles = 30
    fake = FakeGitRun(remote_head="9f8e7d6")  # != local abc123 → advanced
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        assert handler._saturation_heartbeat_active() is False
        assert handler._empty_cycles == 0  # reset → normal frequency resumes
    finally:
        mod.subprocess.run = orig_run


def test_saturation_heartbeat_false_below_threshold(tmp_path):
    """Below threshold → normal interval (remote state irrelevant)."""
    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    handler._empty_cycles = 10
    assert handler._saturation_heartbeat_active() is False
    assert handler._empty_cycles == 10


def test_saturated_tick_still_runs_full_cycle(tmp_path):
    """Saturated handler runs a full cycle (never skipped) at heartbeat."""
    from emrg.server import scheduler as mod

    handler, captured = _make_cycle_handler(tmp_path, frames=[
        {"request_id": "r1", "content": "Done", "done": True,
         "delta": False, "session_id": "s"},
    ])
    handler._empty_cycles = 30  # saturated
    fake = FakeGitRun(remote_head="abc123")  # unchanged → stay saturated
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        asyncio.run(handler._run_evolution_cycle())
    finally:
        mod.subprocess.run = orig_run
    assert "log" in captured, "saturated tick must still run a full cycle"
    assert handler._empty_cycles == 31, \
        "NTE cycle during saturation keeps incrementing (heartbeat continues)"


def test_remote_advanced_false_without_git_repo(tmp_path):
    """Not a git repo / ls-remote fails → False (stay saturated, no crash)."""
    from emrg.server import scheduler as mod

    handler = _make_handler(tmp_path, project="", path=str(tmp_path))
    fake = FakeGitRun(git_repo=False)
    orig_run = mod.subprocess.run
    mod.subprocess.run = fake
    try:
        assert handler._remote_advanced() is False
    finally:
        mod.subprocess.run = orig_run


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
