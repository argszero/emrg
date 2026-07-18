"""Tests for skills.loader — frontmatter parser and context builder."""

from pathlib import Path

from emrg.skills.loader import Skill, _parse_frontmatter, build_skills_context


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
