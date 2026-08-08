"""Tests for the installable-skills catalog (rant 2026-08-08T10:14:29).

The catalog is itself a skill (skill-catalog.md) — the existing loader
picks it up, the system prompt is untouched (zero j2 change). Covers:
baseline parsing, embedded-baseline == shipped-file invariant, daemon
startup fallback, catalog-as-skill loading, deprecated recommended.md
skip, host-confirmed install flow (positive/negative), managed-only
update (positive/negative), and corrupt-state tolerance.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import emrg.skills.installer as installer
import emrg.skills.registry as registry
from emrg.skills.loader import _parse_frontmatter, _parse_skill_file, load_skills
from emrg.skills.registry import (
    BASELINE_CATALOG_MD,
    ensure_catalog_file,
    find_catalog_skill,
    load_catalog_skills,
    parse_skills_frontmatter,
    read_state,
    skill_is_managed,
    write_state,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CATALOG = REPO_ROOT / "emrg" / "skills" / "skill-catalog.md"


# ── helpers ──────────────────────────────────────────────────────────

class FakeRunner:
    """Controllable asyncio subprocess runner (FakeGitRun-style)."""

    def __init__(self, skill_output: str = "", cli_install_rc: int = 0):
        self.calls: list[list[str]] = []
        self.skill_output = skill_output
        self.cli_install_rc = cli_install_rc

    async def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0] == "uv" and cmd[1:3] == ["tool", "install"]:
            return installer.CmdResult(self.cli_install_rc, "installed\n")
        if cmd == ["browser-harness", "skill"]:
            return installer.CmdResult(0, self.skill_output)
        return installer.CmdResult(0, "")


class FakeHttp:
    """Controllable api.github.com release responder."""

    def __init__(self, tag: str | None = "v0.1.8"):
        self.tag = tag

    async def __call__(self, url):
        if url.endswith("/releases/latest"):
            if self.tag is None:
                return None
            return {"tag_name": self.tag}
        return None


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Point config_dir() at a temp dir for all catalog paths.

    Patches BOTH the registry module's and the installer module's
    config_dir binding (installer._resolve_dest consults its own), so no
    test ever touches the real ~/.emrg/skills/.
    """
    import emrg.skills.loader as loader

    # mimic the real config_dir() shape (~/.emrg) so all paths line up
    emrg_dir = tmp_path / ".emrg"
    monkeypatch.setattr(registry, "config_dir", lambda: emrg_dir)
    monkeypatch.setattr(installer, "config_dir", lambda: emrg_dir)
    # isolate the loader's user-skill dir (~/.emrg/skills) from the real host
    monkeypatch.setattr(loader.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


@pytest.fixture
def no_cli(monkeypatch):
    monkeypatch.setattr(installer, "cli_available", lambda: False)


@pytest.fixture
def with_cli(monkeypatch):
    monkeypatch.setattr(installer, "cli_available", lambda: True)


def _run(coro):
    return asyncio.run(coro)


VALID_SKILL_MD = """---
name: browser-harness
description: "Direct browser control via CDP: automation, scraping, testing, site work."
---

# browser-harness

Body text.
"""


# ── catalog parsing ──────────────────────────────────────────────────

class TestParseFrontmatter:
    def test_baseline_parses_one_entry_with_all_fields(self):
        entries = parse_skills_frontmatter(BASELINE_CATALOG_MD)
        assert len(entries) == 1
        e = entries[0]
        assert e["name"] == "browser-harness"
        assert e["description"].startswith("Direct browser control via CDP")
        assert e["repo"] == "browser-use/browser-harness"
        assert e["install"] == "self-publishing"
        assert e["dest"] == "~/.emrg/skills/"
        assert e["check"] == "github_release"

    def test_embedded_baseline_matches_shipped_file(self):
        shipped = SHIPPED_CATALOG.read_text(encoding="utf-8")
        assert shipped == BASELINE_CATALOG_MD

    def test_quoted_description_with_inner_colon_kept(self):
        entries = parse_skills_frontmatter(BASELINE_CATALOG_MD)
        assert entries[0]["description"] == (
            "Direct browser control via CDP: automation, scraping, testing, site work."
        )

    def test_missing_fields_filtered_out(self):
        text = """---
name: skill-catalog
description: "d"
skills:
  - name: only-name
---
"""
        assert parse_skills_frontmatter(text) == []

    def test_garbage_returns_empty(self):
        assert parse_skills_frontmatter("no frontmatter here") == []
        assert parse_skills_frontmatter("---\nnot: yaml\n---\n") == []

    def test_empty_skills_list(self):
        text = "---\nname: skill-catalog\ndescription: d\nskills:\n---\nbody"
        assert parse_skills_frontmatter(text) == []


class TestCatalogFile:
    def test_ensure_writes_baseline_when_missing(self, tmp_home):
        path = ensure_catalog_file()
        assert path.exists()
        assert path.read_text(encoding="utf-8") == BASELINE_CATALOG_MD

    def test_ensure_does_not_overwrite_existing(self, tmp_home):
        path = registry.catalog_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: custom-catalog\n---\n", encoding="utf-8")
        ensure_catalog_file()
        assert path.read_text(encoding="utf-8").startswith("---\nname: custom-catalog")

    def test_load_catalog_from_disk(self, tmp_home):
        ensure_catalog_file()
        entries = load_catalog_skills()
        assert [e["name"] for e in entries] == ["browser-harness"]
        assert find_catalog_skill("browser-harness") is not None
        assert find_catalog_skill("nope") is None

    def test_load_catalog_missing_file(self, tmp_home):
        assert load_catalog_skills() == []

    def test_corrupt_catalog_file_tolerated(self, tmp_home):
        path = registry.catalog_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\x00\x01garbage", encoding="utf-8")
        assert load_catalog_skills() == []


# ── catalog IS a skill (revised design core) ─────────────────────────

class TestCatalogAsSkill:
    def test_parse_skill_file_loads_catalog(self):
        # acceptance 1: the catalog itself is a normal skill
        skill = _parse_skill_file(SHIPPED_CATALOG, "user")
        assert skill is not None
        assert skill.name == "skill-catalog"
        assert "installable" in skill.description

    def test_load_skills_includes_catalog(self, tmp_home):
        # user skills dir: catalog + one real skill → both loaded
        skills_dir = tmp_home / ".emrg" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "skill-catalog.md").write_text(BASELINE_CATALOG_MD, encoding="utf-8")
        real = skills_dir / "real-skill.md"
        real.write_text(VALID_SKILL_MD.replace("browser-harness", "real-skill"), encoding="utf-8")
        skills = load_skills(project_dir=tmp_home)
        names = [s.name for s in skills]
        assert "skill-catalog" in names
        assert "real-skill" in names

    def test_deprecated_recommended_md_never_loads(self, tmp_home):
        # the superseded registry file (10:11:35 design) must not become a skill
        skills_dir = tmp_home / ".emrg" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "recommended.md").write_text(
            "---\nskills:\n  - name: browser-harness\n---\nbody", encoding="utf-8"
        )
        skills = load_skills(project_dir=tmp_home)
        assert [s.name for s in skills] == []

    def test_system_j2_untouched(self):
        # acceptance 1/5: zero template change — no new section, no catalog mention
        j2 = (REPO_ROOT / "emrg" / "server" / "prompts" / "system.j2").read_text(encoding="utf-8")
        assert "Recommended Skills" not in j2
        assert "skill-catalog" not in j2
        assert "browser-harness" not in j2


# ── state ────────────────────────────────────────────────────────────

class TestState:
    def test_write_read_roundtrip(self, tmp_home):
        write_state({"browser-harness": {"version": "0.1.8", "managed": True}})
        state = read_state()
        assert state["browser-harness"]["version"] == "0.1.8"
        assert skill_is_managed("browser-harness")
        assert not skill_is_managed("other")

    def test_corrupt_state_tolerated(self, tmp_home):
        path = registry.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        assert read_state() == {}

    def test_non_dict_state_tolerated(self, tmp_home):
        path = registry.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[1,2,3]", encoding="utf-8")
        assert read_state() == {}

    def test_state_write_is_atomic_no_tmp_leftover(self, tmp_home):
        write_state({"a": {"managed": True}})
        leftovers = [f for f in os.listdir(registry.state_path().parent) if f.endswith(".tmp")]
        assert leftovers == []


# ── install flow ─────────────────────────────────────────────────────

class TestInstall:
    def test_unknown_skill_errors(self, tmp_home, no_cli):
        result = _run(installer.install_skill("nope"))
        assert "error" in result

    def test_requires_confirmation_when_cli_missing(self, tmp_home, no_cli):
        ensure_catalog_file()
        result = _run(installer.install_skill("browser-harness", confirmed=False))
        assert result["confirm_required"] is True
        assert "uv tool install" in result["install_command"]

    def test_full_install_flow(self, tmp_home, with_cli):
        ensure_catalog_file()
        runner = FakeRunner(skill_output=VALID_SKILL_MD)
        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp("v0.1.8")
        ))
        assert result["ok"] is True
        assert result["version"] == "0.1.8"
        # skill file landed with valid frontmatter
        skill_file = tmp_home / ".emrg" / "skills" / "browser-harness.md"
        assert skill_file.exists()
        fm = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        assert fm["name"] == "browser-harness" and fm["description"]
        # state recorded managed
        state = read_state()
        assert state["browser-harness"]["managed"] is True
        assert state["browser-harness"]["version"] == "0.1.8"
        # publish step ran exactly once
        assert runner.calls.count(["browser-harness", "skill"]) == 1

    def test_cli_installed_when_confirmed(self, tmp_home, no_cli, monkeypatch):
        ensure_catalog_file()
        runner = FakeRunner(skill_output=VALID_SKILL_MD)

        def _cli_available():
            return len(runner.calls) >= 1  # after uv install, CLI "exists"

        monkeypatch.setattr(installer, "cli_available", _cli_available)
        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp("v0.1.8")
        ))
        assert result["ok"] is True
        assert any(c[0] == "uv" and c[1:3] == ["tool", "install"] for c in runner.calls)

    def test_cli_install_failure(self, tmp_home, no_cli, monkeypatch):
        ensure_catalog_file()
        runner = FakeRunner(skill_output=VALID_SKILL_MD, cli_install_rc=1)
        monkeypatch.setattr(installer, "cli_available", lambda: False)
        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp()
        ))
        assert "error" in result
        assert not (tmp_home / ".emrg" / "skills" / "browser-harness.md").exists()

    def test_publish_invalid_output_rolls_back(self, tmp_home, with_cli):
        ensure_catalog_file()
        # skill output without name/description frontmatter → refused, no file
        runner = FakeRunner(skill_output="# just a title\nno frontmatter\n")
        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp()
        ))
        assert "error" in result
        assert not (tmp_home / ".emrg" / "skills" / "browser-harness.md").exists()
        assert read_state() == {}  # nothing recorded

    def test_publish_empty_output(self, tmp_home, with_cli):
        ensure_catalog_file()
        runner = FakeRunner(skill_output="")
        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp()
        ))
        assert "error" in result

    def test_install_version_fallback_when_api_down(self, tmp_home, with_cli):
        ensure_catalog_file()
        runner = FakeRunner(skill_output=VALID_SKILL_MD)
        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp(tag=None)
        ))
        assert result["ok"] is True
        assert result["version"] == "unknown"


# ── update check ─────────────────────────────────────────────────────

class TestUpdate:
    def _seed_state(self, state: dict):
        """Seed .state.json with an explicit dict (hyphenated skill names
        cannot be Python kwargs)."""
        write_state(state)

    def test_updates_managed_skill_on_new_release(self, tmp_home, with_cli):
        ensure_catalog_file()
        self._seed_state({"browser-harness": {"version": "0.1.3", "installed_at": "2026-08-08T00:00:00+08:00", "managed": True}})
        runner = FakeRunner(skill_output=VALID_SKILL_MD)
        result = _run(installer.update_managed_skills(runner=runner, http_get=FakeHttp("v0.1.8")))
        assert result["updated"] == ["browser-harness"]
        assert read_state()["browser-harness"]["version"] == "0.1.8"
        assert (tmp_home / ".emrg" / "skills" / "browser-harness.md").exists()

    def test_up_to_date_no_publish(self, tmp_home, with_cli):
        ensure_catalog_file()
        self._seed_state({"browser-harness": {"version": "0.1.8", "installed_at": "2026-08-08T00:00:00+08:00", "managed": True}})
        runner = FakeRunner(skill_output=VALID_SKILL_MD)
        result = _run(installer.update_managed_skills(runner=runner, http_get=FakeHttp("v0.1.8")))
        assert result["updated"] == []
        assert runner.calls == []

    def test_api_down_skips_silently(self, tmp_home, with_cli):
        ensure_catalog_file()
        self._seed_state({"browser-harness": {"version": "0.1.3", "installed_at": "2026-08-08T00:00:00+08:00", "managed": True}})
        runner = FakeRunner(skill_output=VALID_SKILL_MD)
        result = _run(installer.update_managed_skills(runner=runner, http_get=FakeHttp(tag=None)))
        assert result["updated"] == []
        assert read_state()["browser-harness"]["version"] == "0.1.3"

    def test_skips_when_cli_missing(self, tmp_home, no_cli):
        ensure_catalog_file()
        self._seed_state({"browser-harness": {"version": "0.1.3", "installed_at": "2026-08-08T00:00:00+08:00", "managed": True}})
        result = _run(installer.update_managed_skills(http_get=FakeHttp("v0.1.8")))
        assert result["skipped"] == ["browser-harness"]
        assert read_state()["browser-harness"]["version"] == "0.1.3"

    def test_manual_copies_untouched(self, tmp_home, with_cli):
        ensure_catalog_file()
        # browser-harness is NOT in state (host manual copy) → never refreshed
        write_state({"other": {"version": "9.9.9", "managed": True}})
        manual = tmp_home / ".emrg" / "skills" / "browser-harness.md"
        manual.parent.mkdir(parents=True, exist_ok=True)
        manual.write_text("# host's own copy\n", encoding="utf-8")
        result = _run(installer.update_managed_skills(runner=FakeRunner(VALID_SKILL_MD), http_get=FakeHttp("v0.1.8")))
        assert "browser-harness" not in result["updated"]
        assert manual.read_text(encoding="utf-8") == "# host's own copy\n"

    def test_unknown_repo_entry_ignored(self, tmp_home, with_cli):
        ensure_catalog_file()
        # entry not in catalog anymore → skipped
        write_state({"ghost": {"version": "0.1.0", "managed": True}})
        result = _run(installer.update_managed_skills(runner=FakeRunner(VALID_SKILL_MD), http_get=FakeHttp("v0.9.0")))
        assert result["checked"] == 1
        assert result["updated"] == []

    def test_update_error_reported(self, tmp_home, with_cli):
        ensure_catalog_file()
        self._seed_state({"browser-harness": {"version": "0.1.3", "installed_at": "2026-08-08T00:00:00+08:00", "managed": True}})
        runner = FakeRunner(skill_output="broken output")
        result = _run(installer.update_managed_skills(runner=runner, http_get=FakeHttp("v0.1.8")))
        assert result["errors"] == ["browser-harness"]
        assert read_state()["browser-harness"]["version"] == "0.1.3"  # not advanced

    def test_no_state_noop(self, tmp_home, with_cli):
        ensure_catalog_file()
        result = _run(installer.update_managed_skills(runner=FakeRunner(VALID_SKILL_MD), http_get=FakeHttp("v0.1.8")))
        assert result == {"checked": 0, "updated": [], "skipped": [], "errors": []}
