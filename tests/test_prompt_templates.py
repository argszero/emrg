"""Wiring guards for the task-type prompt templates.

Why this file exists
--------------------
The daemon renders every task prompt through jinja2 with
``undefined=jinja2.Undefined`` (see ``TaskHandler._build_evolution_prompt``). That
choice is deliberate — the old ``str.format()`` crashed on a missing placeholder —
but it has a sharp edge: a name that is not in the builder's context renders as an
**empty string** instead of raising. A typo therefore breaks nothing a test can see.
The prompt simply tells the agent to write into a blank path, or to follow a blank
instruction, and the whole suite stays green.

These tests close that hole from both ends:

1. ``test_every_prompt_template_renders_through_the_real_builder`` — every built-in
   task type's template renders through the real builder (real context, real env)
   with a *minimal* task config, so the ``{% if ... %}``-guarded optional sections
   take their unset branch, and no unrendered ``{{ }}`` / ``{% %}`` tag is left.
2. ``test_no_prompt_names_a_placeholder_the_builder_does_not_provide`` — the same
   templates re-rendered with ``StrictUndefined``, which *raises* on a missing name,
   against the context the builder actually produced. That is the check which turns
   a silent blank into a failure. The strict renderer is self-tested with a planted
   typo, so a device that silently stops detecting anything cannot pass.

Named limit: this pins the wiring, not the prose. It cannot show that an agent
follows the procedure, that optional fields (``{% if task.extra_prompt %}``,
``{% if project.description %}``) are set in the host's config, or that the prompt's
content is any good.
"""

from __future__ import annotations

import re
from pathlib import Path

import jinja2
import yaml

from emrg.protocol import InstanceIdentity
from emrg.server import scheduler as mod
from emrg.server.scheduler import TaskHandler
from emrg.tools import bash_tool

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = REPO_ROOT / "emrg" / "server"

# A reference to a variable or a block tag that survived rendering.
LEFTOVER_TAG = re.compile(r"\{\{|\{%")

# Optional task/project fields the prompts guard on. Populated only by the strict
# render, so that a "missing" name there is a typo rather than a design choice.
FULL_TASK_CONFIG: dict = {
    "project": "demoproj",
    "role": "committer",
    "author_id": "argszero",
    "keywords": "demo, wiring",
    "extra_prompt": "EXTRA-PROMPT-MARKER",
    "allow_self_merge": True,
}
FULL_PROJECT_ENTRY: dict = {
    "name": "demoproj",
    "description": "PROJECT-DESCRIPTION-MARKER",
}


def _write_projects_yml(tmp_path: Path, name: str, entry: dict) -> Path:
    project_dir = tmp_path / name
    project_dir.mkdir(exist_ok=True)
    row = {"path": str(project_dir)}
    row.update(entry)
    (tmp_path / "projects.yml").write_text(
        yaml.safe_dump([row]), encoding="utf-8"
    )
    return project_dir


def _make_handler(
    tmp_path: Path,
    monkeypatch,
    template_name: str,
    config: dict,
    project_entry: dict | None = None,
) -> TaskHandler:
    """A real handler, from a hermetic config dir, pointed at a real template."""
    monkeypatch.setattr(mod, "config_dir", lambda: tmp_path)
    name = config.get("project", "demoproj")
    _write_projects_yml(tmp_path, name, project_entry or {"name": name})
    return TaskHandler(
        name="demo-task",
        config=config,
        interval=300,
        identity=InstanceIdentity(),
        template_path=PROMPTS_DIR / template_name,
    )


def _builtin_templates() -> list[tuple[str, str]]:
    return sorted(mod.TASK_TEMPLATES.items())


def test_every_prompt_template_renders_through_the_real_builder(tmp_path, monkeypatch) -> None:
    """Each built-in task type renders, with the optional sections unset."""
    minimal = {"project": "demoproj"}
    for task_type, filename in _builtin_templates():
        handler = _make_handler(tmp_path, monkeypatch, filename, minimal)
        rendered = handler._build_evolution_prompt()
        leftover = LEFTOVER_TAG.search(rendered)
        assert leftover is None, (
            f"{task_type}/{filename}: unrendered {leftover.group()!r} left in the "
            f"prompt (Jinja saw it as text, so the agent would too)"
        )
        assert len(rendered) > 500, (
            f"{task_type}/{filename}: only {len(rendered)} chars rendered — "
            f"almost everything came out empty"
        )


def test_no_prompt_names_a_placeholder_the_builder_does_not_provide(
    tmp_path, monkeypatch
) -> None:
    """StrictUndefined: a name the builder does not provide fails loudly.

    This is the guard that the daemon's own ``Undefined`` cannot give: rendering the
    real templates against the real context with an env that raises on a missing
    name. Without it, ``{{ evolution_cwd_typo }}`` renders as ``''`` and every test
    in the suite stays green.
    """
    real_env = jinja2.Environment
    captured: dict = {}

    class _RecordingTemplate:
        def __init__(self, inner: jinja2.Template) -> None:
            self._inner = inner

        def render(self, *args, **kwargs) -> str:
            captured.update(kwargs)
            return self._inner.render(*args, **kwargs)

    class _RecordingEnvironment(real_env):  # type: ignore[misc,valid-type]
        def from_string(self, source, *args, **kwargs):
            inner = real_env.from_string(self, source, *args, **kwargs)
            return _RecordingTemplate(inner)

    monkeypatch.setattr(jinja2, "Environment", _RecordingEnvironment)
    handler = _make_handler(
        tmp_path,
        monkeypatch,
        "evolution_prompt.md",
        dict(FULL_TASK_CONFIG),
        dict(FULL_PROJECT_ENTRY),
    )
    handler._build_evolution_prompt()
    monkeypatch.setattr(jinja2, "Environment", real_env)

    # Self-test of the device: if the capture silently stopped working, the strict
    # render below would pass for the wrong reason (nothing to check).
    assert len(captured) >= 15, (
        f"captured only {len(captured)} context keys from the builder — the "
        f"recording environment is not in the render path, so this test would "
        f"prove nothing"
    )
    assert "task" in captured and "project" in captured

    strict = real_env(undefined=jinja2.StrictUndefined)
    with_typo = strict.from_string("{{ evolution_cwd_typo }}")
    try:
        with_typo.render(**captured)
    except jinja2.UndefinedError:
        pass
    else:
        raise AssertionError(
            "the strict renderer did not raise on a planted typo — the check "
            "below would be vacuous"
        )

    for task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        try:
            strict.from_string(text).render(**captured)
        except jinja2.UndefinedError as exc:
            raise AssertionError(
                f"{task_type}/{filename} references a name the builder does not "
                f"provide ({exc}); with the daemon's Undefined it would silently "
                f"render as an empty string"
            ) from exc


# `{{ evolution_cwd }}` followed by the rest of the path it names.
EVOLUTION_CWD_REF = re.compile(r"\{\{\s*evolution_cwd\s*\}\}([^\s`)\"'|,;]*)")

# A path suffix that routes through a `memory/` directory.
_MEMORY_SEGMENT = re.compile(r"(?:^|/)memory/")


def _evolution_memory_refs(text: str) -> list[str]:
    """Path suffixes of ``{{ evolution_cwd }}`` references that name a memory dir."""
    return [
        suffix
        for suffix in EVOLUTION_CWD_REF.findall(text)
        if _MEMORY_SEGMENT.search(suffix)
    ]


def test_prompt_memory_writes_land_where_the_sandbox_allows_them() -> None:
    """A prompt's memory path must be one the ``workspace-write`` sandbox trusts.

    Measured 2026-09-14 (rant 2026-09-14T14:35:47): `open_source_prompt.md` sent the
    agent's identity file and its "key findings" to `{{ evolution_cwd }}/memory/`,
    i.e. `~/.emrg/evolution/memory/`. That is out of the sandbox's boundary — every
    such write is refused with "blocked write outside workspace", which the rant
    reports an open-source task hitting 32 times in one day — and it is the wrong
    root anyway: the evolution data root is `{{ evolution_cwd }}/.emrg/`, the only
    part of `{{ evolution_cwd }}` `bash_tool._trusted_write_zones()` trusts and the
    only memory the daemon loads. The directory exists, so a write that gets through
    by a route the command-line scan cannot see (an `open()` inside a heredoc, as the
    rant documents) lands in a folder no memory loader reads.

    The expected location is taken from the sandbox's own trust list rather than
    copied here, so if that list moves, this test reports the prompts may be stale
    instead of agreeing with a second copy of the rule.

    Named limit: only ``{{ evolution_cwd }}``-rooted *memory* paths are checked.
    Other paths rooted at `{{ evolution_cwd }}` are the state-file / reflection-file
    mechanism, which the same rant deletes wholesale; that removal carries its own
    guard and this one deliberately does not pre-empt it.
    """
    root = Path(mod.EVOLUTION_CWD)
    zones = bash_tool._trusted_write_zones()
    assert zones, "no trusted write zone — this check would pass vacuously"

    # Self-test of the device: it must reject the exact shape the rant measured,
    # and the machine must actually consider that shape outside the boundary.
    planted = _evolution_memory_refs("write `{{ evolution_cwd }}/memory/identity.md`")
    assert planted == ["/memory/identity.md"], planted
    assert not any(
        bash_tool._is_within(str(root / "memory/identity.md"), zone) for zone in zones
    ), (
        "`~/.emrg/evolution/memory/` is inside a trusted zone on this machine, so "
        "this test cannot discriminate between the two roots here"
    )

    checked = 0
    for task_type, filename in _builtin_templates():
        text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
        for suffix in _evolution_memory_refs(text):
            target = str(root / suffix.lstrip("/"))
            assert any(
                bash_tool._is_within(target, zone) for zone in zones
            ), (
                f"{task_type}/{filename}: tells the agent to write {target!r}, "
                f"which the workspace-write sandbox blocks (trusted zones: {zones}); "
                f"the evolution memory root is `{{{{ evolution_cwd }}}}/.emrg/memory/`"
            )
            checked += 1
    assert checked >= 2, (
        f"only {checked} memory path(s) found across the templates — the scan is "
        f"not looking at what it thinks it is"
    )
