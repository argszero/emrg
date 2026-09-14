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


def test_the_prompt_states_the_memory_limits_the_store_enforces(tmp_path, monkeypatch) -> None:
    """A prompt's memory numbers are the store's constants, not a second spelling.

    Measured 2026-09-14 (`cyc20260914-125119`): `open_source_prompt.md` told the agent
    "title ≤512 chars ... if the index exceeds ~50 entries", while the store warns at
    `INDEX_TITLE_MAX_CHARS = 512` and `INDEX_COUNT_WARN = 100`. The entry number was
    simply wrong — and the same defect class as #1217/#1218/#1219: a stated number that
    is not the one that fires. The prompt now renders both from the context, which is
    built from the constants.

    The discriminator is the one those PRs settled: if the text reads the constant,
    tuning the constant has to move the text. A literal — the state this test was
    written against — cannot follow, so the tuned arm is what makes it load bearing.
    """
    from emrg.memory import INDEX_COUNT_WARN, INDEX_TITLE_MAX_CHARS

    def render() -> str:
        handler = _make_handler(
            tmp_path, monkeypatch, "open_source_prompt.md", {"project": "demoproj"}
        )
        return handler._build_evolution_prompt()

    as_shipped = render()
    assert f"≤{INDEX_TITLE_MAX_CHARS} chars" in as_shipped, (
        "the prompt must state the title limit the store truncates at"
    )
    assert f"exceeds {INDEX_COUNT_WARN} entries" in as_shipped, (
        "the prompt must state the entry count the store warns at"
    )
    assert "~50 entries" not in as_shipped, (
        "the prompt still carries the number the store never used"
    )

    # Tuned: a re-spelled number stays put, a derived one follows.
    monkeypatch.setattr(mod, "INDEX_TITLE_MAX_CHARS", 256)
    monkeypatch.setattr(mod, "INDEX_COUNT_WARN", 42)
    tuned = render()
    assert "≤256 chars" in tuned and f"exceeds 42 entries" in tuned, (
        "the prompt did not follow the tuned constants — its numbers are literals, "
        "so they can drift from the store silently"
    )
    assert "≤512 chars" not in tuned and "exceeds 100 entries" not in tuned

