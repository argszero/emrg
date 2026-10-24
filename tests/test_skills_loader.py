"""Tests for skills.loader — frontmatter parser, file parser, and context builder."""

import os
import sys

import pytest
from pathlib import Path

from tests.bounded_read import DEADLINE_SECONDS, within
from emrg.skills import loader as loader_mod
from emrg.skills.loader import (
    Skill,
    _parse_frontmatter,
    _parse_skill_file,
    build_skills_context,
    load_skills,
    load_skills_with_shadows,
)


def _skill_md(name: str, description: str) -> str:
    """A minimal well-formed skill file declaring ``name``."""
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nbody\n"


class TestParseFrontmatter:
    """Tests for _parse_frontmatter — the minimal YAML frontmatter parser."""

    def test_empty(self):
        """Empty string returns empty dict."""
        assert _parse_frontmatter("") == {}

    def test_single_key_value(self):
        """Basic key-value pair."""
        result = _parse_frontmatter("name: test-skill")
        assert result == {"name": "test-skill"}

    def test_multiple_keys(self):
        """Multiple key-value pairs are all parsed."""
        text = "name: skill-a\ndescription: A useful skill\nversion: 1.0"
        result = _parse_frontmatter(text)
        assert result["name"] == "skill-a"
        assert result["description"] == "A useful skill"
        assert result["version"] == "1.0"

    def test_quoted_values(self):
        """Values in double-quotes are stripped."""
        result = _parse_frontmatter('name: "my skill"')
        assert result["name"] == "my skill"

    def test_single_quoted_values(self):
        """Values in single-quotes are also stripped."""
        result = _parse_frontmatter("name: 'my skill'")
        assert result["name"] == "my skill"

    def test_comments_ignored(self):
        """Lines starting with # are ignored."""
        text = "# this is a comment\nname: real-skill\n# another comment"
        result = _parse_frontmatter(text)
        assert result == {"name": "real-skill"}

    def test_blank_lines_ignored(self):
        """Blank lines are ignored."""
        text = "name: skill-a\n\ndescription: desc"
        result = _parse_frontmatter(text)
        assert len(result) == 2

    def test_no_colon_line(self):
        """Lines without colons are ignored."""
        text = "just some text\nname: skill-a"
        result = _parse_frontmatter(text)
        assert result == {"name": "skill-a"}

    def test_value_with_spaces_unquoted(self):
        """Unquoted values with spaces are kept as-is."""
        result = _parse_frontmatter("description: A multi word description")
        assert result["description"] == "A multi word description"


class TestParseSkillFile:
    """Tests for _parse_skill_file — skill .md file parsing with frontmatter."""

    def test_valid_skill_file(self, tmp_path):
        """A well-formed skill file returns a Skill with correct fields."""
        md = tmp_path / "my-skill.md"
        md.write_text(
            "---\n"
            "name: my-skill\n"
            "description: Does useful things\n"
            "---\n"
            "# Instructions\n"
            "Follow these steps.\n"
        )
        result = _parse_skill_file(md, "user")
        assert result is not None
        assert result.name == "my-skill"
        assert result.description == "Does useful things"
        assert result.source == "user"
        assert result.path == md
        assert "Instructions" in result.body
        assert "Follow these steps" in result.body

    def test_name_from_stem_fallback(self, tmp_path):
        """When frontmatter has no name, falls back to filename stem."""
        md = tmp_path / "fallback-skill.md"
        md.write_text(
            "---\n"
            "description: A skill without explicit name\n"
            "---\n"
            "# Body\n"
        )
        result = _parse_skill_file(md, "agent")
        assert result is not None
        assert result.name == "fallback-skill"

    def test_no_frontmatter(self, tmp_path):
        """File without --- delimiters returns None."""
        md = tmp_path / "no-fm.md"
        md.write_text("# Just a markdown file\n\nNo frontmatter here.\n")
        result = _parse_skill_file(md, "user")
        assert result is None

    def test_malformed_frontmatter_single_delimiter(self, tmp_path):
        """File with only one --- returns None (malformed)."""
        md = tmp_path / "bad.md"
        md.write_text("---\nname: s\n# no closing ---\n")
        result = _parse_skill_file(md, "user")
        assert result is None

    def test_empty_frontmatter(self, tmp_path):
        """Empty frontmatter (nothing between --- and ---) returns None."""
        md = tmp_path / "empty-fm.md"
        md.write_text("---\n---\n# Body after empty frontmatter\n")
        result = _parse_skill_file(md, "user")
        assert result is None

    def test_missing_description(self, tmp_path):
        """Frontmatter without description returns None."""
        md = tmp_path / "no-desc.md"
        md.write_text(
            "---\n"
            "name: nameless\n"
            "---\n"
            "# Body\n"
        )
        result = _parse_skill_file(md, "user")
        assert result is None

    def test_file_not_found(self, tmp_path):
        """Non-existent file returns None."""
        md = tmp_path / "does-not-exist.md"
        result = _parse_skill_file(md, "user")
        assert result is None

    def test_source_flag(self, tmp_path):
        """Source is correctly propagated ('user' or 'agent')."""
        md = tmp_path / "src-test.md"
        md.write_text(
            "---\n"
            "name: src-test\n"
            "description: Testing source propagation\n"
            "---\n"
            "# Body\n"
        )
        assert _parse_skill_file(md, "user").source == "user"
        assert _parse_skill_file(md, "agent").source == "agent"

    def test_body_stripped(self, tmp_path):
        """Leading/trailing whitespace in body is stripped."""
        md = tmp_path / "body-test.md"
        md.write_text(
            "---\n"
            "name: body-test\n"
            "description: Testing body trimming\n"
            "---\n"
            "\n"
            "  # Instructions with surrounding whitespace  \n"
            "\n"
        )
        result = _parse_skill_file(md, "user")
        assert result is not None
        assert result.body == "# Instructions with surrounding whitespace"

    def test_unicode_decode_error(self, tmp_path):
        """Binary file content returns None (UnicodeDecodeError)."""
        md = tmp_path / "binary.md"
        md.write_bytes(b"\xff\xfe\x00\x00")
        result = _parse_skill_file(md, "user")
        assert result is None


class TestBuildSkillsContext:
    """Tests for build_skills_context — system prompt skills summary."""

    def test_empty_skills(self):
        """Empty skills list returns empty string."""
        assert build_skills_context([]) == ""

    @pytest.mark.skipif(sys.platform == "win32", reason="/tmp path is POSIX-only")
    def test_single_skill(self):
        """Single skill generates a summary with name, source, path, description."""
        skill = Skill(
            name="test-skill",
            description="Does something useful",
            path=Path("/tmp/test.md"),
            body="## Instructions\nDo this.",
            source="user",
        )
        result = build_skills_context([skill])
        assert "## Available Skills" in result
        assert "test-skill" in result
        assert "user" in result
        assert "/tmp/test.md" in result
        assert "Does something useful" in result

    def test_multiple_skills(self):
        """Multiple skills are each listed."""
        a = Skill(
            name="skill-a", description="Desc A",
            path=Path("/tmp/a.md"), body="body", source="user",
        )
        b = Skill(
            name="skill-b", description="Desc B",
            path=Path("/tmp/b.md"), body="body", source="agent",
        )
        result = build_skills_context([a, b])
        assert "skill-a" in result
        assert "skill-b" in result
        assert "Desc A" in result
        assert "Desc B" in result

    def test_progressive_disclosure_instruction(self):
        """The summary includes the progressive disclosure instruction."""
        skill = Skill(
            name="s", description="desc",
            path=Path("/tmp/s.md"), body="body", source="user",
        )
        result = build_skills_context([skill])
        assert "read tool" in result
        assert "follow its instructions" in result


class TestADeclarationThatLosesItsName:
    """Two declarations of one name: the loser is named, not swallowed (issue #2563).

    The loader's two layers key a skill by different things — `_skill_files_in` selects
    candidate *files* by filename, `_skills_under` files the parsed result under the
    **declared** `name:` — so two files that declare one name meet only in the merge,
    where the later arrival replaces the earlier one. Within a root "later" means
    "sorts later by filename", a rule nothing in the module promises; across roots it
    is the documented precedence that the agent's copy beats the user's.

    Either way one declaration stops being offered, and the only trace used to be a
    `logger.debug` line whose stream this deployment discards (`stderr=DEVNULL`), so
    the loss reached no reader. Each test below pins the skill that *does* load as
    well: naming a loser must not be bought by dropping the winner, and a collision
    must not turn into a crash.
    """

    @staticmethod
    def _roots(tmp_path, monkeypatch):
        """A user root and an agent root, both off the real home."""
        home = tmp_path / "home" / ".emrg"
        (home / "skills").mkdir(parents=True)
        monkeypatch.setattr(loader_mod, "config_dir", lambda: home)
        agent = tmp_path / "agent"
        (agent / ".emrg" / "skills").mkdir(parents=True)
        return home / "skills", agent

    def test_two_flat_files_declaring_one_name_report_the_one_that_lost_it(
        self, tmp_path, monkeypatch
    ):
        """Flat/flat in one root: both declare `dup`, and `a.md` is named as the loser."""
        user, agent = self._roots(tmp_path, monkeypatch)
        (user / "a.md").write_text(_skill_md("dup", "from a"), encoding="utf-8")
        (user / "z.md").write_text(_skill_md("dup", "from z"), encoding="utf-8")

        skills, shadows = load_skills_with_shadows(agent_dir=agent)

        assert [s.name for s in skills] == ["dup"], "one name must still load as one skill"
        assert skills[0].description == "from z", (
            "which declaration wins is not this fix's business — it stays the one "
            f"filename order picks, and it changed: {skills[0].description!r}"
        )
        assert [(s.name, s.dropped.path.name, s.kept.path.name) for s in shadows] == [
            ("dup", "a.md", "z.md")
        ], shadows

    def test_two_bundles_declaring_one_name_report_the_one_that_lost_it(
        self, tmp_path, monkeypatch
    ):
        """Bundle/bundle in one root: `alpha/` is named as the loser of `shared-name`."""
        user, agent = self._roots(tmp_path, monkeypatch)
        for bundle in ("alpha", "beta"):
            (user / bundle).mkdir()
            (user / bundle / "SKILL.md").write_text(
                _skill_md("shared-name", f"from {bundle}"), encoding="utf-8"
            )

        skills, shadows = load_skills_with_shadows(agent_dir=agent)

        assert [s.name for s in skills] == ["shared-name"]
        assert skills[0].description == "from beta", skills[0].description
        assert [(s.dropped.path.parent.name, s.kept.path.parent.name) for s in shadows] == [
            ("alpha", "beta")
        ], shadows

    def test_the_agent_copy_shadows_the_user_copy_and_says_which_it_beat(
        self, tmp_path, monkeypatch
    ):
        """User/agent: the documented precedence keeps its answer, and is now reportable.

        The answer is unchanged — this is the one case the module already stated, so a
        fix that moved it would be a regression dressed as a repair. What is new is
        that the user's shadowed copy is named rather than dropped.
        """
        user, agent = self._roots(tmp_path, monkeypatch)
        (user / "deploy.md").write_text(_skill_md("deploy", "the user copy"), encoding="utf-8")
        (agent / ".emrg" / "skills" / "deploy.md").write_text(
            _skill_md("deploy", "the agent copy"), encoding="utf-8"
        )

        skills, shadows = load_skills_with_shadows(agent_dir=agent)

        assert len(skills) == 1, skills
        assert skills[0].source == "agent", "the documented cross-root precedence changed"
        assert skills[0].description == "the agent copy"
        assert [(s.kept.source, s.dropped.source) for s in shadows] == [("agent", "user")]

    def test_distinct_names_report_no_shadow(self, tmp_path, monkeypatch):
        """The control: a report that invents a loser where there is none is the mirror fault."""
        user, agent = self._roots(tmp_path, monkeypatch)
        (user / "one.md").write_text(_skill_md("one", "first"), encoding="utf-8")
        (user / "two.md").write_text(_skill_md("two", "second"), encoding="utf-8")

        skills, shadows = load_skills_with_shadows(agent_dir=agent)

        assert [s.name for s in skills] == ["one", "two"]
        assert shadows == [], shadows

    def test_the_context_names_the_loser_and_is_unchanged_without_one(
        self, tmp_path, monkeypatch
    ):
        """The rendered summary names both files, and adds nothing when nothing collided."""
        user, agent = self._roots(tmp_path, monkeypatch)
        (user / "a.md").write_text(_skill_md("dup", "from a"), encoding="utf-8")
        (user / "z.md").write_text(_skill_md("dup", "from z"), encoding="utf-8")

        skills, shadows = load_skills_with_shadows(agent_dir=agent)
        rendered = build_skills_context(skills, shadows)

        assert "Shadowed names" in rendered
        assert "a.md" in rendered, "the declaration that lost the name is not in the summary"
        assert "z.md" in rendered, "the declaration in effect is not in the summary"
        assert "Shadowed names" not in build_skills_context(skills, [])
        assert build_skills_context(skills) == build_skills_context(skills, [])


class TestASkillDirectoryWalkedByKind:
    """A skill file that is not a regular file is skipped, not opened (issue #2083).

    `read_text` on a FIFO blocks at the *open* until a writer appears, so the loader's
    `except (OSError, UnicodeDecodeError)` cannot bound it: nothing is raised and the call
    never returns. Every caller of `load_skills` runs on the daemon's event loop — the
    `skills_install` branch, the auto-update loop, and startup — so the block is the whole
    server. Measured 2026-07-11-style on master `3386f239` (`cyc20261011-070311`): a `mkfifo`
    named `wedged.md` beside a usable skill made `load_skills(agent_dir)` **not return in
    8 s**, where the same fixture without it returned in 0.00 s.

    The agent skills directory is under the *workspace*, the agent's own write surface,
    which is what makes it reachable rather than theoretical.
    """

    GOOD = "---\nname: good\ndescription: a usable skill\n---\n\nbody\n"

    @staticmethod
    def _agent(tmp_path, fifo: bool):
        skills = tmp_path / ".emrg" / "skills"
        skills.mkdir(parents=True)
        (skills / "good.md").write_text(TestASkillDirectoryWalkedByKind.GOOD)
        if fifo:
            os.mkfifo(skills / "wedged.md")
        return tmp_path

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_a_fifo_beside_a_skill_does_not_wedge_the_loader(self, tmp_path):
        """The measurement: the walk returns, and the usable skill still loads.

        Both halves matter. A guard that aborted the load on the odd entry would pass
        "returns" while losing every skill in the directory, which is the same loss the
        hang caused one step earlier.
        """
        agent = self._agent(tmp_path, fifo=True)
        skills = within(DEADLINE_SECONDS, lambda: load_skills(agent_dir=agent))
        names = [s.name for s in skills]
        assert "good" in names, f"the usable skill was lost: {names}"
        assert "wedged" not in names, f"a FIFO was loaded as a skill: {names}"

    def test_the_control_without_the_fifo(self, tmp_path):
        """Same fixture, no pipe: the same reading, so the pipe is what was measured."""
        agent = self._agent(tmp_path, fifo=False)
        names = [s.name for s in load_skills(agent_dir=agent)]
        assert "good" in names, names
        assert "wedged" not in names, names

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="this platform has no FIFOs")
    def test_parse_skill_file_skips_it_the_way_it_skips_an_unreadable_one(self, tmp_path):
        """At the one open site: `None` and a named kind, not a refusal.

        The loader's own answer to an entry it cannot use is `None` (an unparseable file,
        a missing description, a deprecated registry file). A special file is one more of
        those — refusing here would turn one odd file into a failed system-prompt build.
        """
        fifo = tmp_path / "wedged.md"
        os.mkfifo(fifo)
        assert within(DEADLINE_SECONDS, lambda: _parse_skill_file(fifo, "agent")) is None
