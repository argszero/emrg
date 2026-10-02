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
    Indented (nested) lines are ignored so a skill whose frontmatter also
    carries a nested metadata list (skill-catalog.md, rant
    2026-08-08T10:14:29) keeps its own top-level name/description —
    nested ``description:`` keys must not overwrite the top-level one.
    This avoids adding pyyaml as a dependency for the simple format.
    """
    result: dict[str, str] = {}
    for line in text.split("\n"):
        if line[:1].isspace():  # indented → nested YAML, not a top-level key
            continue
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


def frontmatter_block(text: str) -> tuple[str, str] | None:
    """The frontmatter block and the body, or ``None`` when there is no block.

    **The delimiters are lines.** A YAML frontmatter block starts and ends with a
    line that is exactly ``---``, which is the rule ``emrg/memory.py`` has always
    used for the memory files (``lines[0].strip() == "---"`` … ``lines[i].strip()
    == "---"``). This module read the delimiter as a **substring** instead
    (``text.split("---", 2)``), so a ``---`` *inside a value* ended the block
    early — silently, and in a way no reader could see:

        ---
        name: tripledash
        description: Use --- to separate sections
        ---
        # Body

    measured on master: the description becomes ``"Use"`` and the body becomes
    ``"to separate sections\\n---\\n# Body"`` — the rest of the description leaks
    into the body, and since progressive disclosure shows the model *only* the
    description, the skill is advertised as doing something it does not.

    One home, three readers. ``emrg/skills/registry.py`` (the catalog's ``skills:``
    list) and ``emrg/skills/installer.py`` (the published-file check) each carried
    their own copy of the substring split; the installer also handed the parser the
    **whole file** rather than the block, so a body line spelled ``description: …``
    satisfied its "has frontmatter" check while ``load_skills`` rejected the same
    file — install reported ``{"ok": True}`` for a skill that can never load. All
    three now ask this function.

    :param text: the file's text.
    :returns: ``(frontmatter, body)``, both stripped, or ``None`` when the text
        does not begin with a ``---`` line or has no closing ``---`` line.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]).strip(), "\n".join(lines[i + 1 :]).strip()
    return None


def _parse_skill_file(file_path: Path, source: str) -> Optional[Skill]:
    """Parse a single skill .md file. Returns None if parsing fails."""
    # Defensive: the deprecated registry file (superseded 2026-08-08T10:14:29
    # by skill-catalog.md) would parse as a bogus skill named "recommended"
    # — never load it as one.
    if file_path.name == "recommended.md":
        logger.debug("skill: skipping deprecated registry file %s", file_path)
        return None

    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        logger.debug("skill: cannot read %s", file_path)
        return None

    block = frontmatter_block(text)
    if block is None:
        logger.debug("skill: no frontmatter block in %s", file_path)
        return None

    frontmatter_str, body = block

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
        "The following skills are available. When the user asks what skills "
        "you have or to list your skills, list the skills below by name "
        "and description (do not make up tools). When a skill seems relevant "
        "to the user's request, use the read tool to read the skill file "
        "at the listed path, then follow its instructions.",
        "",
    ]
    for s in skills:
        lines.append(f"- **{s.name}** ({s.source}, `{s.path}`): {s.description}")

    return "\n".join(lines)
