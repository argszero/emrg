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

# The "is this path a regular file" rule is the tool layer's, reached at a path. Imported
# rather than restated, for the reason `emrg/memory.py` gives at the same import: the
# boundary is one fact, and a second spelling of it is a second boundary that can drift.
from emrg.tools.base import non_regular_kind

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


def _parse_skill_file(file_path: Path, source: str) -> Optional[Skill]:
    """Parse a single skill .md file. Returns None if parsing fails."""
    # Defensive: the deprecated registry file (superseded 2026-08-08T10:14:29
    # by skill-catalog.md) would parse as a bogus skill named "recommended"
    # — never load it as one.
    if file_path.name == "recommended.md":
        logger.debug("skill: skipping deprecated registry file %s", file_path)
        return None

    # ── Ask the subject's kind before opening it ──
    #
    # `read_text` on a FIFO blocks at the *open* until a writer appears, and on a socket
    # until a connection does — so the `except (OSError, UnicodeDecodeError)` below cannot
    # bound the consequence. Nothing is raised, the call simply never returns, and every
    # caller of this function runs on the daemon's event loop (`load_skills` is called from
    # `_handle_message`'s install branch, the skills auto-update loop, and startup), so the
    # block is the whole server. Measured 2026-10-11 (`cyc20261011-070311`): a `mkfifo`
    # named `wedged.md` beside a usable skill made `load_skills(project_dir)` **not return
    # in 8 s**, while the same fixture without it returned in 0.00 s.
    #
    # The project directory is the agent's own write surface (`.emrg/skills/` under the
    # workspace), which is what makes this reachable rather than theoretical — the same
    # argument `#2076` made for `.emrg/memory/`.
    #
    # Skipping rather than refusing, because this loop walks a bag of *optional* files and
    # already answers an entry it cannot use by skipping it: the `except` below returns
    # `None` for an unreadable or undecodable file so the other skills still load. Refusing
    # here would turn one odd file into a failed system-prompt build, and the answer must
    # not be harsher than the one this function already gives. The kind is named in the log
    # at the same level as "cannot read", so a skill that fails to appear is diagnosable
    # rather than silent.
    try:
        kind = non_regular_kind(file_path)
    except OSError:
        # A missing path: `non_regular_kind` stats its subject and `stat` raises, which is
        # this function's documented answer already — `None`, "if parsing fails" — and the
        # same answer the `read_text` below gave before the kind was asked. Named here so
        # the missing-file reading keeps saying the same thing it always said.
        logger.debug("skill: cannot read %s", file_path)
        return None
    if kind:
        logger.debug("skill: skipping %s — it is %s, not a regular file", file_path, kind)
        return None
    try:
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
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
