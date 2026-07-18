"""Skill loader — parse Markdown skills with YAML frontmatter.

Skills are filesystem-based Markdown packages. They live in:
  - ~/.emrg/skills/*.md    (user skills)
  - .emrg/skills/*.md      (project skills)

Each skill file has YAML frontmatter:
  ---
  name: skill-name
  description: What this skill does
  ---
  # Markdown body with instructions

The loader uses a minimal hand-written frontmatter parser to avoid
adding a YAML dependency for the simple key-value format.

Progressive disclosure: only the skill name + description are injected
into the system prompt. The LLM uses the read tool to load the full
body when it decides a skill is relevant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """A loaded skill from a Markdown file."""
    name: str
    description: str
    path: Path          # source file path
    body: str           # markdown body (after frontmatter)
    source: str         # "user" or "project"


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple key: value YAML frontmatter.

    Handles quoted strings and plain values. No nested structures.
    This avoids adding pyyaml as a dependency for the simple format.
    """
    result: dict[str, str] = {}
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result


def _parse_skill_file(file_path: Path, source: str) -> Optional[Skill]:
    """Parse a single skill .md file. Returns None if parsing fails."""
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception:
        logger.debug("skill: cannot read %s", file_path)
        return None

    if not text.startswith("---"):
        logger.debug("skill: no frontmatter in %s", file_path)
        return None

    # Split on --- delimiters: first ---, then frontmatter, then ---, then body
    parts = text.split("---", 2)
    if len(parts) < 3:
        logger.debug("skill: malformed frontmatter in %s", file_path)
        return None

    frontmatter_str = parts[1].strip()
    body = parts[2].strip()

    fm = _parse_frontmatter(frontmatter_str)
    if not fm:
        return None

    name = fm.get("name", file_path.stem)
    description = fm.get("description", "")
    if not description:
        logger.debug("skill: no description in %s", file_path)
        return None

    return Skill(
        name=name,
        description=description,
        path=file_path,
        body=body,
        source=source,
    )


def load_skills(project_dir: Optional[Path] = None) -> list[Skill]:
    """Load all skills from user and project directories.

    User skills (~/.emrg/skills/) are loaded first, then project skills.
    Skills with duplicate names: project overrides user.
    """
    if project_dir is None:
        project_dir = Path.cwd()

    all_skills: dict[str, Skill] = {}

    # User skills (lower priority)
    user_dir = Path.home() / ".emrg" / "skills"
    if user_dir.is_dir():
        for md_file in sorted(user_dir.glob("*.md")):
            skill = _parse_skill_file(md_file, "user")
            if skill:
                all_skills[skill.name] = skill
                logger.debug("skill loaded: %s (user)", skill.name)

    # Project skills (higher priority — override user)
    proj_dir = project_dir / ".emrg" / "skills"
    if proj_dir.is_dir():
        for md_file in sorted(proj_dir.glob("*.md")):
            skill = _parse_skill_file(md_file, "project")
            if skill:
                all_skills[skill.name] = skill
                logger.debug("skill loaded: %s (project)", skill.name)

    return sorted(all_skills.values(), key=lambda s: s.name)


def build_skills_context(skills: list[Skill]) -> str:
    """Build the skill summary text for the system prompt.

    Only includes name + description — the full body is loaded
    on-demand by the LLM using the read tool (progressive disclosure).
    """
    if not skills:
        return ""

    lines = [
        "## Available Skills",
        "",
        "The following skills are available. When a skill seems relevant "
        "to the user's request, use the read tool to read the skill file "
        "at the listed path, then follow its instructions.",
        "",
    ]
    for s in skills:
        lines.append(f"- **{s.name}** ({s.source}, `{s.path}`): {s.description}")

    return "\n".join(lines)
