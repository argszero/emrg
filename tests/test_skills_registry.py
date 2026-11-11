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

from tests.bounded_read import DEADLINE_SECONDS, within
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
    def test_baseline_parses_its_entries_with_all_fields(self):
        entries = parse_skills_frontmatter(BASELINE_CATALOG_MD)
        assert [e["name"] for e in entries] == ["browser-harness", "skill-creator"]
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
        assert [e["name"] for e in entries] == ["browser-harness", "skill-creator"]
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
        skills = load_skills(agent_dir=tmp_home)
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
        skills = load_skills(agent_dir=tmp_home)
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


# ── the catalog and the state file are read by kind, not by opening ──
#
# `exists()` is true for a FIFO, which is why it is not a kind: a reader that tested it and
# then opened its subject had nothing bounding the open, and `read_text` on a FIFO waits at
# the *open* for a writer and raises nothing — so no handler runs and the call never returns.
# Both readers below run on the daemon's event loop (`skills_available`, `skills_install`),
# so the block is the whole server. Measured on master `94dcdae7` (2026-10-11,
# `cyc20261011-100220`): each FIFO made its reader **not return in 8 s**, where a regular
# file returned in 0.00 s.
#
# The reads are bounded by `tests.bounded_read.within`, the one deadline this suite keeps
# (issue #2091): the bound is what makes a regression *fail* instead of wedging the run.


class TestTheRegistryAsksTheKindBeforeOpening:
    """A FIFO at either path is skipped, not opened (issue #2093)."""

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_fifo_catalog_is_skipped_rather_than_opened(self, tmp_home):
        """Returns the empty answer it gives a missing catalog — and it returns at all."""
        path = registry.catalog_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        assert within(DEADLINE_SECONDS, load_catalog_skills) == []

    def test_the_control_catalog_without_the_fifo(self, tmp_home):
        """Same fixture, a regular file: the catalog reads, so the kind is the axis."""
        ensure_catalog_file()
        assert [e["name"] for e in load_catalog_skills()] == [
            "browser-harness",
            "skill-creator",
        ]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_fifo_state_file_is_skipped_rather_than_opened(self, tmp_home):
        path = registry.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        assert within(DEADLINE_SECONDS, read_state) == {}
        # the reader's other caller goes through the same open
        assert within(DEADLINE_SECONDS, lambda: skill_is_managed("browser-harness")) is False

    def test_the_control_state_file_without_the_fifo(self, tmp_home):
        write_state({"browser-harness": {"managed": True}})
        assert read_state() == {"browser-harness": {"managed": True}}
        assert skill_is_managed("browser-harness") is True

    def test_a_missing_subject_is_still_the_empty_answer(self, tmp_home):
        """What the kind check must not narrow: absent is empty, not an exception.

        `non_regular_kind` stats its subject and `stat` raises on a missing path, so this
        contract is the one the fix had to keep while dropping `path.exists()`.
        """
        assert load_catalog_skills() == []
        assert read_state() == {}
        assert skill_is_managed("anything") is False


# ── the publisher asks the target's kind (issue #2101) ───────────────

class TestThePublisherAsksTheKindBeforeOpening:
    """`_publish_skill` wrote its target without asking what it was.

    `Path.write_text` is `open(path, "w")`, and opening a FIFO for **writing** waits for a
    *reader* — the opposite peer to the read case, so no guard elsewhere in this family would
    have covered it. Measured 2026-10-11 (`cyc20261011-130932`) on master `3f18f6a6`, one child
    process per arm under an 8 s cap: a `mkfifo` at `<dest>/probe.md` left `_publish_skill`
    **not returned in 8 s**, where the same fixture with a regular file returned in 0.00 s with
    `{"ok": True, ...}`. `skills_install` runs on the daemon's event loop, so the block is the
    server.

    The answer is the refusal this function already gives a failed write — `NotARegularFile` is
    an `OSError`, so it lands in the `except OSError` already written there and comes back as
    `{"error": "cannot write skill file: …"}` — now with the kind named.

    The FIFO arm carries an `os.mkfifo` gate, so it skips where there are no FIFOs; the arm
    that covers those platforms is `test_a_directory_target_is_refused_by_kind_on_every_platform`,
    which is FIFO-free **and discriminating** (see its docstring for the two-way measurement).
    """

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_fifo_target_is_refused_rather_than_blocking(self, tmp_home, with_cli):
        ensure_catalog_file()
        os.mkfifo(tmp_home / ".emrg" / "skills" / "browser-harness.md")
        runner = FakeRunner(skill_output=VALID_SKILL_MD)

        result = within(DEADLINE_SECONDS, lambda: _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp("v0.1.8")
        )))

        assert "error" in result, result
        assert "cannot write skill file" in result["error"], result["error"]
        assert "regular file" in result["error"], result["error"]
        assert read_state() == {}, "nothing may be recorded when the publish did not happen"

    def test_the_control_with_a_regular_target(self, tmp_home, with_cli):
        """The kind is the axis: the same call with no FIFO anywhere still publishes."""
        ensure_catalog_file()
        runner = FakeRunner(skill_output=VALID_SKILL_MD)

        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp("v0.1.8")
        ))

        assert result["ok"] is True, result
        assert (tmp_home / ".emrg" / "skills" / "browser-harness.md").is_file()

    def test_a_directory_target_is_refused_by_kind_on_every_platform(self, tmp_home, with_cli):
        """**FIFO-free, and it discriminates** — the arm the Windows leg was missing.

        The FIFO arm above is gated on `os.mkfifo`, so it skips wherever there are none
        (Windows), and the other arms here pass with *and* without the guard: measured
        2026-10-11 on this file with `emrg/skills/installer.py` reverted to `3f18f6a6`,
        the FIFO-free half is green either way — so the suite's earlier claim that "the
        Windows leg runs the discriminating half" was false, and the fix is to give that
        leg an arm that really does discriminate rather than to weaken the sentence.

        A **directory** is the arm that works on every platform, and it is the one
        `tests/test_config.py` uses for the same reason (issue #2103 / PR #2104): both
        versions refuse the write, and only one of them refuses it **by name**. The
        assertion is therefore on the name, not on the failure:

        ==================  =========================================================
        base `3f18f6a6`     ``cannot write skill file: [Errno 21] Is a directory: …``
        this tree           ``cannot write skill file: … is a directory, not a regular file``
        ==================  =========================================================

        The `IsADirectoryError`/`PermissionError` text differs by platform, which is
        exactly why pinning it would not have survived Windows; ``not a regular file`` is
        this family's own wording and is what the base cannot produce.
        """
        ensure_catalog_file()
        (tmp_home / ".emrg" / "skills" / "browser-harness.md").mkdir()
        runner = FakeRunner(skill_output=VALID_SKILL_MD)

        result = _run(installer.install_skill(
            "browser-harness", confirmed=True, runner=runner, http_get=FakeHttp("v0.1.8")
        ))

        assert "error" in result, result
        assert "cannot write skill file" in result["error"], result["error"]
        assert "not a regular file" in result["error"], result["error"]


# ── the catalog has one source file (issue #2809) ────────────────────


class TestTheCatalogHasOneSourceFile:
    """One producer for the shipped catalog.

    `packaging/assets/skill-catalog.md` was a **third** copy: `build-runtime.sh` copied it
    into `dist/assets/` and nothing in the tree read the result — the daemon seeds the
    host's catalog from `BASELINE_CATALOG_MD` in `EmrgServer.__init__`, which runs before
    any session, so the copy had no window in which it was the only catalog present.

    Two carriers that must agree are held byte-for-byte by
    ``test_embedded_baseline_matches_shipped_file``. A third carrier held to nothing can
    only **disagree**: a host who opened the runtime's copy read a list the binary never
    consulted, and every change to the two that matter left it further behind.

    Measured 2026-11-11 before the fix: the search below answered
    ``['emrg/skills/skill-catalog.md', 'packaging/assets/skill-catalog.md']``.
    """

    def test_the_repository_holds_exactly_one_catalog_source_file(self):
        found = sorted(
            p.relative_to(REPO_ROOT).as_posix()
            for top in ("emrg", "packaging", "scripts")
            for p in (REPO_ROOT / top).rglob("skill-catalog.md")
        )
        assert found == ["emrg/skills/skill-catalog.md"], (
            f"a second catalog source file exists: {found} — the catalog has one producer, "
            "and a copy nothing reads can only disagree with it (issue #2809)"
        )

    def test_the_runtime_build_ships_no_catalog_copy(self):
        """The other half: the file being absent is no help if the build still copies one."""
        script = (REPO_ROOT / "packaging" / "build-runtime.sh").read_text(encoding="utf-8")
        copies = [
            line.strip()
            for line in script.splitlines()
            if "skill-catalog" in line
            and line.strip().startswith(("cp ", "install ", "rsync ", "ditto "))
        ]
        assert copies == [], f"the runtime build copies a catalog again: {copies}"
# ── vendor-copy: a catalog skill that ships as a plain directory (issue #2807) ──────


VENDOR_CATALOG = """---
name: skill-catalog
description: "test catalog"
skills:
  - name: vendor-one
    description: "a skill published as a directory"
    repo: example/vendor-one
    source: skills/vendor-one
    install: vendor-copy
    dest: ~/.emrg/skills/
    check: github_commit
---

# Catalog
"""

VENDOR_SKILL_MD = "---\nname: vendor-one\ndescription: a vendored skill\n---\n\n# body\n"


def _write_catalog(text: str) -> None:
    """Put a catalog on disk where every reader in this module looks for it."""
    path = registry.catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tarball(paths: dict[str, str], root: str = "skills-abc1234") -> bytes:
    """A repo tarball as GitHub serves it: one top-level `<repo>-<sha>/` directory."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in paths.items():
            data = text.encode("utf-8")
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeBytes:
    """Controllable tarball responder, and the URLs it was asked for."""

    def __init__(self, payload: bytes | None):
        self.payload = payload
        self.urls: list[str] = []

    async def __call__(self, url: str):
        self.urls.append(url)
        return self.payload


class FakeCommits:
    """api.github.com responder for a path's last commit.

    Answers the *releases* endpoint too, and with a tag, because the point of these tests is
    that a `github_commit` entry must never be read as a release.
    """

    def __init__(self, sha: str | None = "abc1234def5678"):
        self.sha = sha
        self.urls: list[str] = []

    async def __call__(self, url: str):
        self.urls.append(url)
        if "/commits" in url:
            return None if self.sha is None else [{"sha": self.sha}]
        if url.endswith("/releases/latest"):
            return {"tag_name": "v9.9.9"}
        return None


class TestVendorCopyInstall:
    """`install: vendor-copy` — a source directory copied, with no executable installer.

    The catalog could carry only `self-publishing` before this, and `_publish_skill` refuses
    any other value, so an entry for a skill that upstream publishes as a plain directory
    could be listed and never installed.
    """

    def test_a_vendor_copy_entry_copies_its_source_directory(self, tmp_home, no_cli):
        _write_catalog(VENDOR_CATALOG)
        payload = _tarball({
            "skills/vendor-one/SKILL.md": VENDOR_SKILL_MD,
            "skills/vendor-one/scripts/run.py": "print('vendored')\n",
            "skills/neighbour/SKILL.md": "---\nname: neighbour\ndescription: d\n---\n",
        })
        fetcher = FakeBytes(payload)
        http = FakeCommits("abc1234def5678")

        result = _run(installer.install_skill(
            "vendor-one", http_get=http, fetch_bytes=fetcher
        ))

        assert result["ok"] is True, result
        target = tmp_home / ".emrg" / "skills" / "vendor-one"
        assert (target / "SKILL.md").read_text(encoding="utf-8") == VENDOR_SKILL_MD
        assert (target / "scripts" / "run.py").read_text(encoding="utf-8") == "print('vendored')\n"
        assert not (target / "neighbour").exists(), "a sibling directory was copied too"
        assert read_state()["vendor-one"]["managed"] is True
        assert read_state()["vendor-one"]["version"] == "abc1234def5678"

    def test_a_vendor_copy_needs_no_cli_on_path(self, tmp_home, no_cli):
        """The half the entry alone would break: `install_skill` used to demand the CLI first."""
        _write_catalog(VENDOR_CATALOG)
        fetcher = FakeBytes(_tarball({"skills/vendor-one/SKILL.md": VENDOR_SKILL_MD}))
        runner = FakeRunner()

        result = _run(installer.install_skill(
            "vendor-one", confirmed=False, runner=runner, http_get=FakeCommits(),
            fetch_bytes=fetcher,
        ))

        assert result.get("confirm_required") is None, result
        assert result["ok"] is True, result
        assert runner.calls == [], f"a vendor copy ran a subprocess: {runner.calls}"

    def test_the_version_is_the_source_commit_not_a_release_tag(self, tmp_home, no_cli):
        _write_catalog(VENDOR_CATALOG)
        http = FakeCommits("0f1e2d3c4b5a6978")

        result = _run(installer.install_skill(
            "vendor-one", http_get=http,
            fetch_bytes=FakeBytes(_tarball({"skills/vendor-one/SKILL.md": VENDOR_SKILL_MD})),
        ))

        assert result["version"] == "0f1e2d3c4b5a6978", result
        assert any("path=skills/vendor-one" in u for u in http.urls), http.urls
        assert not any(u.endswith("/releases/latest") for u in http.urls), http.urls

    def test_an_archive_without_the_source_directory_is_refused(self, tmp_home, no_cli):
        _write_catalog(VENDOR_CATALOG)
        payload = _tarball({"skills/somebody-else/SKILL.md": VENDOR_SKILL_MD})

        result = _run(installer.install_skill(
            "vendor-one", http_get=FakeCommits(), fetch_bytes=FakeBytes(payload),
        ))

        assert "error" in result, result
        assert "skills/vendor-one" in result["error"], result["error"]
        assert not (tmp_home / ".emrg" / "skills" / "vendor-one").exists()
        assert read_state() == {}, "nothing may be recorded when the copy did not happen"

    def test_a_copy_without_a_usable_skill_file_is_refused_and_leaves_nothing(
        self, tmp_home, no_cli
    ):
        """The validation this shares with the publisher: name+description, or nothing."""
        _write_catalog(VENDOR_CATALOG)
        payload = _tarball({
            "skills/vendor-one/SKILL.md": "# no frontmatter at all\n",
            "skills/vendor-one/scripts/run.py": "print(1)\n",
        })

        result = _run(installer.install_skill(
            "vendor-one", http_get=FakeCommits(), fetch_bytes=FakeBytes(payload),
        ))

        assert "error" in result, result
        target = tmp_home / ".emrg" / "skills" / "vendor-one"
        assert not target.exists(), "a refused copy left the half-written directory behind"
        assert read_state() == {}

    def test_a_download_that_fails_is_refused(self, tmp_home, no_cli):
        _write_catalog(VENDOR_CATALOG)
        result = _run(installer.install_skill(
            "vendor-one", http_get=FakeCommits(), fetch_bytes=FakeBytes(None),
        ))
        assert "error" in result, result
        assert not (tmp_home / ".emrg" / "skills" / "vendor-one").exists()

    def test_an_unknown_install_kind_is_still_refused(self, tmp_home, no_cli):
        """The control: accepting a second kind must not mean accepting any kind."""
        _write_catalog(VENDOR_CATALOG.replace("vendor-copy", "telepathy"))
        result = _run(installer.install_skill(
            "vendor-one", http_get=FakeCommits(),
            fetch_bytes=FakeBytes(_tarball({"skills/vendor-one/SKILL.md": VENDOR_SKILL_MD})),
        ))
        assert "error" in result, result
        assert "unsupported install kind" in result["error"], result["error"]

    def test_the_update_check_moves_a_vendor_copy_by_commit(self, tmp_home, no_cli):
        _write_catalog(VENDOR_CATALOG)
        write_state({"vendor-one": {"managed": True, "version": "older000"}})
        http = FakeCommits("newer111")
        fetcher = FakeBytes(_tarball({"skills/vendor-one/SKILL.md": VENDOR_SKILL_MD}))

        result = _run(installer.update_managed_skills(
            http_get=http, fetch_bytes=fetcher
        ))

        assert result["updated"] == ["vendor-one"], result
        assert result["errors"] == [], result
        assert read_state()["vendor-one"]["version"] == "newer111"
        assert any("path=skills/vendor-one" in u for u in http.urls), http.urls
        assert not any(u.endswith("/releases/latest") for u in http.urls), http.urls

    def test_the_update_check_leaves_a_vendor_copy_whose_commit_did_not_move(
        self, tmp_home, no_cli
    ):
        """A vendor copy is refreshed by a moved commit, never on every tick."""
        _write_catalog(VENDOR_CATALOG)
        write_state({"vendor-one": {"managed": True, "version": "abc1234def5678"}})
        fetcher = FakeBytes(_tarball({"skills/vendor-one/SKILL.md": VENDOR_SKILL_MD}))

        result = _run(installer.update_managed_skills(
            http_get=FakeCommits("abc1234def5678"), fetch_bytes=fetcher
        ))

        assert result["updated"] == [], result
        assert fetcher.urls == [], "the archive was fetched although nothing moved"


class TestTheCatalogOffersWhatItShips:
    """The on-disk catalog is an overlay; the binary's baseline is what ships.

    `ensure_catalog_file()` writes the baseline only when the file is **absent**, so a host
    whose file predates an entry never sees it: measured 2026-11-11, `~/.emrg/skills/`
    carried a 1,198-byte catalog naming no `skill-creator`.  Read as a whole replacement,
    the shipped list would reach fresh installs and no others.
    """

    def _disk(self, entries: str) -> None:
        _write_catalog(
            "---\nname: skill-catalog\ndescription: \"host edited\"\nskills:\n"
            f"{entries}---\n\n# Catalog\n"
        )

    def test_a_shipped_entry_the_disk_file_lacks_is_still_offered(self, tmp_home):
        self._disk(
            "  - name: browser-harness\n"
            '    description: "d"\n    repo: browser-use/browser-harness\n'
            "    install: self-publishing\n    dest: ~/.emrg/skills/\n"
            "    check: github_release\n"
        )
        names = [e["name"] for e in load_catalog_skills()]
        assert names == ["browser-harness", "skill-creator"], names
        assert find_catalog_skill("skill-creator") is not None

    def test_a_disk_entry_wins_over_the_baseline_entry_of_the_same_name(self, tmp_home):
        self._disk(
            "  - name: browser-harness\n"
            '    description: "mine"\n    repo: someone/fork\n'
            "    install: self-publishing\n    dest: ~/.emrg/skills/\n"
            "    check: github_release\n"
        )
        entries = {e["name"]: e for e in load_catalog_skills()}
        assert entries["browser-harness"]["repo"] == "someone/fork", entries["browser-harness"]
        assert [e["name"] for e in load_catalog_skills()].count("browser-harness") == 1

    def test_an_absent_catalog_file_is_still_the_empty_answer(self, tmp_home):
        """The control: an overlay has to be an overlay *of something present*.

        A deleted catalog is a host opting out, and the daemon writes the baseline at its
        next start — so this answer stays empty rather than growing a list of its own.
        """
        assert load_catalog_skills() == []

    def test_the_baseline_carries_the_vendor_copy_entry(self):
        entries = {e["name"]: e for e in parse_skills_frontmatter(BASELINE_CATALOG_MD)}
        assert "skill-creator" in entries, sorted(entries)
        entry = entries["skill-creator"]
        assert entry["install"] == "vendor-copy"
        assert entry["check"] == "github_commit"
        assert entry["repo"] == "anthropics/skills"
        assert entry["source"] == "skills/skill-creator"
