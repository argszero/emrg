"""Tests for skills.loader — frontmatter parser, file parser, and context builder."""

import os
from pathlib import Path

from emrg.skills.loader import Skill, _parse_frontmatter, _parse_skill_file, build_skills_context


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
        result = _parse_skill_file(md, "project")
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
        """Source is correctly propagated ('user' or 'project')."""
        md = tmp_path / "src-test.md"
        md.write_text(
            "---\n"
            "name: src-test\n"
            "description: Testing source propagation\n"
            "---\n"
            "# Body\n"
        )
        assert _parse_skill_file(md, "user").source == "user"
        assert _parse_skill_file(md, "project").source == "project"

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
            path=Path("/tmp/b.md"), body="body", source="project",
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
