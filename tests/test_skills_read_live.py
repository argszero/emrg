"""Skills are read live, per session, from both roots and in both layouts.

Three defects, measured on `5c9ed63b` before this file existed (rant
2026-10-21T09:30:39):

1. `EmrgServer.__init__` kept `self.skills`, a **snapshot** taken at daemon start and
   refreshed only by three unrelated events. A skill written to `~/.emrg/skills/`
   after start reached no prompt: measured in one session, `subagent-delegate` was
   written at 09:13 and a later prompt carried **0** occurrences of it while
   `skill-catalog`, loaded from the same directory by the same loader, was there.
2. `load_skills()` fell back to `Path.cwd()` — the **daemon's** directory, `~/.emrg` —
   so every session's "project" skills were `~/.emrg/.emrg/skills/` whatever project
   the session was in.
3. Both roots were walked with `glob("*.md")`, so a skill written as the spec's
   `<name>/SKILL.md` was invisible: `~/.emrg/skills/browser-harness/SKILL.md`
   (7,940 B, with `references/` beside it) had never been loaded.

Each test below names a property, not a line: what a session must see, and when.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from emrg.config import LlmConfig
from emrg.server.daemon import EmrgServer
from emrg.session import Session
from emrg.skills import loader as loader_mod
from emrg.skills import registry as registry_mod
from emrg.skills.loader import _project_root, load_skills

SKILL_TEXT = "---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"


def _skill(name: str, description: str = "a skill", body: str = "Body.") -> str:
    return SKILL_TEXT.format(name=name, description=description, body=body)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """A user root and a project root, both off the real home (and both empty)."""
    data = tmp_path / "home" / ".emrg"
    (data / "skills").mkdir(parents=True)
    monkeypatch.setattr(loader_mod, "config_dir", lambda: data)
    monkeypatch.setattr(registry_mod, "config_dir", lambda: data)

    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".emrg" / "skills").mkdir(parents=True)
    return data / "skills", project


# ── the two layouts ────────────────────────────────────────────────


def test_a_flat_file_and_a_directory_bundle_both_load(roots):
    """`<name>.md` and `<name>/SKILL.md` are the same kind of skill, read alike."""
    user_root, _ = roots
    (user_root / "flat-skill.md").write_text(_skill("flat-skill"), encoding="utf-8")
    (user_root / "bundle-skill").mkdir()
    (user_root / "bundle-skill" / "SKILL.md").write_text(
        _skill("bundle-skill", "a bundled skill"), encoding="utf-8"
    )

    names = [s.name for s in load_skills(project_dir=user_root)]
    assert names == ["bundle-skill", "flat-skill"], names


def test_a_bundle_carries_the_directory_its_references_sit_in(roots):
    """The path a prompt prints has to lead a reader to the skill's own files.

    A bundle may ship `references/`, `scripts/` and the like beside `SKILL.md`; the
    path recorded for the skill is that `SKILL.md`, whose parent is the bundle — so a
    reader told to follow the skill can find the rest of it.
    """
    user_root, _ = roots
    bundle = user_root / "with-files"
    (bundle / "references").mkdir(parents=True)
    (bundle / "SKILL.md").write_text(_skill("with-files"), encoding="utf-8")
    (bundle / "references" / "install.md").write_text("details\n", encoding="utf-8")

    skill = next(s for s in load_skills(project_dir=user_root) if s.name == "with-files")
    assert skill.path.name == "SKILL.md"
    assert skill.path.parent == bundle
    assert (skill.path.parent / "references" / "install.md").is_file()


def test_one_name_in_both_layouts_loads_once_and_the_bundle_wins(roots):
    """A root carrying both forms holds one skill, and the bundle is the one kept."""
    user_root, _ = roots
    (user_root / "both.md").write_text(
        _skill("both", "the flat leftover"), encoding="utf-8"
    )
    (user_root / "both").mkdir()
    (user_root / "both" / "SKILL.md").write_text(
        _skill("both", "the bundle"), encoding="utf-8"
    )

    skills = [s for s in load_skills(project_dir=user_root) if s.name == "both"]
    assert len(skills) == 1
    assert skills[0].description == "the bundle"
    assert skills[0].path.name == "SKILL.md"


# ── the project root ───────────────────────────────────────────────


def test_the_project_root_is_the_nearest_git_ancestor(roots):
    """A session opened deep inside a project still sees that project's skills."""
    _, project = roots
    (project / ".emrg" / "skills" / "project-skill.md").write_text(
        _skill("project-skill"), encoding="utf-8"
    )
    deep = project / "src" / "nested"
    deep.mkdir(parents=True)

    assert _project_root(deep) == project
    names = [s.name for s in load_skills(project_dir=deep)]
    assert "project-skill" in names, names


def test_a_directory_with_no_repository_above_it_is_its_own_root(tmp_path):
    """Scratch and temp directories are projects of their own, not somebody's child."""
    scratch = tmp_path / "scratch"
    (scratch / ".emrg" / "skills").mkdir(parents=True)
    (scratch / ".emrg" / "skills" / "here.md").write_text(
        _skill("here"), encoding="utf-8"
    )

    assert _project_root(scratch) == scratch


def test_the_project_beats_the_user_root_on_a_shared_name(roots):
    """Same name in both roots: the project's copy is the one loaded."""
    user_root, project = roots
    (user_root / "shared.md").write_text(
        _skill("shared", "the user copy"), encoding="utf-8"
    )
    (project / ".emrg" / "skills" / "shared.md").write_text(
        _skill("shared", "the project copy"), encoding="utf-8"
    )

    shared = next(s for s in load_skills(project_dir=project) if s.name == "shared")
    assert shared.description == "the project copy"
    assert shared.source == "project"


# ── nothing is cached ──────────────────────────────────────────────


def test_a_skill_written_after_a_load_is_seen_by_the_next_one(roots):
    """Install, edit and delete all reach the next call without any reload step."""
    user_root, _ = roots
    assert [s.name for s in load_skills(project_dir=user_root)] == []

    (user_root / "later.md").write_text(_skill("later", body="first body"), encoding="utf-8")
    assert "later" in [s.name for s in load_skills(project_dir=user_root)]

    (user_root / "later.md").write_text(_skill("later", body="second body"), encoding="utf-8")
    later = next(s for s in load_skills(project_dir=user_root) if s.name == "later")
    assert later.body.endswith("second body"), later.body

    (user_root / "later.md").unlink()
    assert [s.name for s in load_skills(project_dir=user_root)] == []


# ── the daemon reads with the session's directory ──────────────────


def _server(tmp_path: Path) -> EmrgServer:
    """A daemon built the way the daemon tests build one."""
    import tempfile

    server = EmrgServer(LlmConfig(base_url="http://localhost", api_key="test"))
    server._projects_log = Path(tempfile.mkdtemp()) / "projects.yml"
    return server


def test_the_prompt_carries_the_sessions_project_skills(roots, tmp_path):
    """The skills in a prompt come from the session's project, not the daemon's cwd."""
    _, project = roots
    (project / ".emrg" / "skills" / "session-skill.md").write_text(
        _skill("session-skill", "only this project has it"), encoding="utf-8"
    )
    server = _server(tmp_path)
    session = Session.create_with_id("skills-live", project / "src")

    rendered = server._build_system_prompt(session)
    assert "session-skill" in rendered, "the session's own project skill did not reach the prompt"


def test_a_skill_installed_after_the_daemon_started_reaches_the_next_prompt(roots, tmp_path):
    """The defect itself: a snapshot taken at start could never see this skill."""
    _, project = roots
    server = _server(tmp_path)

    before = server._build_system_prompt(Session.create_with_id("skills-late-a", project))
    assert "late-skill" not in before

    (project / ".emrg" / "skills" / "late-skill.md").write_text(
        _skill("late-skill", "written after the daemon started"), encoding="utf-8"
    )

    after = server._build_system_prompt(Session.create_with_id("skills-late-b", project))
    assert "late-skill" in after, "a skill on disk did not reach the next prompt"


def test_the_daemon_keeps_no_skills_snapshot(tmp_path):
    """The snapshot is gone, and nothing may quietly reintroduce it.

    A `hasattr` check rather than a source grep: an attribute that exists again is the
    same defect whatever code fills it, and the two live readers below reach disk.
    """
    server = _server(tmp_path)
    assert not hasattr(server, "skills"), (
        "the daemon holds a skills snapshot again; skills are read live with the "
        "session's cwd, so a copy on the server can only go stale"
    )


# ── skills_available reads disk too ────────────────────────────────


class _FakeWriter:
    """The daemon reaches its client through `ws.send`; nothing else is recorded."""

    def __init__(self) -> None:
        self._frames: list[bytes] = []

    async def send(self, data) -> None:
        self._frames.append(data.encode() if isinstance(data, str) else data)


def _drive(server: EmrgServer, msg: dict) -> list[dict]:
    writer = _FakeWriter()
    asyncio.run(server._process_message(msg, writer))  # type: ignore[arg-type]
    return [json.loads(raw) for raw in writer._frames]


def test_skills_available_answers_from_disk_not_a_start_time_snapshot(roots, tmp_path):
    """The catalog's installed flags follow the files, exactly as the prompt does."""
    user_root, _ = roots
    (user_root / "skill-catalog.md").write_text(
        "---\n"
        "name: skill-catalog\n"
        'description: "installable skills"\n'
        "skills:\n"
        "  - name: installable-one\n"
        '    description: "not installed yet"\n'
        "    repo: example/installable-one\n"
        "    install: self-publishing\n"
        "    dest: ~/.emrg/skills/\n"
        "    check: github_release\n"
        "---\n\n# Catalog\n",
        encoding="utf-8",
    )
    server = _server(tmp_path)

    first = _drive(server, {"type": "skills_available"})
    entries = first[0]["skills"] if first and "skills" in first[0] else []
    assert [e["installed"] for e in entries if e["name"] == "installable-one"] == [False]

    (user_root / "installable-one.md").write_text(
        _skill("installable-one", "now it is here"), encoding="utf-8"
    )

    second = _drive(server, {"type": "skills_available"})
    entries = second[0]["skills"] if second and "skills" in second[0] else []
    assert [e["installed"] for e in entries if e["name"] == "installable-one"] == [True], (
        "skills_available still reports a skill installed after start as missing"
    )
