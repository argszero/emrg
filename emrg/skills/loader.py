"""Skill loader — parse Markdown skills with YAML frontmatter.

Skills are filesystem-based Markdown packages. They live in two roots:

  - ~/.emrg/skills/         (user skills)
  - <agent>/.emrg/skills/ (agent skills)

and each root is read in **both** layouts, `<name>.md` and `<name>/SKILL.md`
(`_skill_files_in`). `<agent>` is the nearest ancestor of the *session's*
working directory carrying a `.git` (`_agent_root`) — not the process's cwd,
which for the daemon is `~/.emrg` and would give every session one agent's
skills.

Nothing here is cached: every `load_skills` call re-reads the files, so a skill
written, edited or deleted on disk reaches the next prompt without a restart.

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

# Where this instance's user skills live is `config_dir()/skills`, the same directory the
# installer publishes into and the catalog is read from — one derivation, three readers,
# and a test can redirect it in one place (`installer._resolve_dest` already does).
from emrg.config import config_dir

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """A loaded skill from a Markdown file."""
    name: str
    description: str
    path: Path          # source file path
    body: str           # markdown body (after frontmatter)
    source: str         # "user" or "agent"


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
    # named `wedged.md` beside a usable skill made `load_skills(agent_dir)` **not return
    # in 8 s**, while the same fixture without it returned in 0.00 s.
    #
    # The agent directory is the agent's own write surface (`.emrg/skills/` under the
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


def _agent_root(start: Path) -> Path:
    """The nearest ancestor of ``start`` carrying a ``.git``, else ``start`` itself.

    A session is normally opened *inside* a agent rather than at its top, while the
    agent's skills sit at the top — so the root has to be resolved rather than
    assumed to be the directory the session happens to be in. A directory with no
    repository above it is its own root, which is what a scratch or temp directory
    wants and what keeps a skills directory beside the files being worked on usable.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return start


def _user_skills_root() -> Path:
    """``~/.emrg/skills`` — the instance-wide root, taken from `config_dir()`."""
    return config_dir() / "skills"


def _skill_files_in(root: Path) -> list[Path]:
    """Every skill file one root carries, in both layouts the ecosystem uses.

    A skill is either a flat ``<root>/<name>.md`` or a bundle ``<root>/<name>/SKILL.md``.
    Both must be read: the flat form is what this instance's installer publishes
    (``installer.install_skill`` writes ``<name>.md``) and what the shipped catalog is,
    while the bundle is the layout the skill spec documents and the only one that can
    carry the files a skill references beside it.

    When one root carries both for the same name the **bundle wins**: it is the form
    that can hold the rest of the skill, so the flat file beside it is a leftover copy
    of the same skill rather than a second one.

    Only the shape is decided here. Whether an entry can be opened at all — a FIFO, a
    socket, a file with no frontmatter — stays `_parse_skill_file`'s question, which is
    why a flat entry is listed without being stat'ed first: a FIFO named `wedged.md`
    must reach the kind check that skips it, not be filtered out here where the skip
    would be silent (issue #2083).
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        logger.debug("skill: cannot list %s", root)
        return []

    flat: dict[str, Path] = {}
    bundled: dict[str, Path] = {}
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if is_dir:
            body = entry / "SKILL.md"
            if body.is_file():
                bundled[entry.name] = body
        elif entry.name.endswith(".md"):
            flat[entry.stem] = entry

    candidates = dict(flat)
    candidates.update(bundled)
    return [candidates[name] for name in sorted(candidates)]


def _skills_under(root: Path, source: str, into: dict[str, Skill]) -> None:
    """Read one root into ``into``, the later root winning on a shared name."""
    for skill_file in _skill_files_in(root):
        skill = _parse_skill_file(skill_file, source)
        if skill:
            into[skill.name] = skill
            logger.debug("skill loaded: %s (%s) from %s", skill.name, source, skill_file)


def load_skills(agent_dir: Optional[Path] = None) -> list[Skill]:
    """Load every skill from the user root and one agent root, read live.

    User skills (``~/.emrg/skills/``) are read first, then the agent's
    (``<root>/.emrg/skills/``); a skill of the same name: the agent's wins.

    ``agent_dir`` is the **session's** working directory — deliberately not the
    process's, which for the daemon is whatever it was started in and made every
    session read the same agent's skills. It resolves to the nearest ancestor
    carrying a ``.git`` (`_agent_root`). Called with nothing, it falls back to the
    process cwd, for a caller that has no session at all.

    Nothing is cached, here or above: every call re-reads the files, so a skill
    installed, edited or deleted on disk reaches the next prompt without a restart.
    The cost is one directory listing and a few small reads (0.074 ms median measured
    for three skills), and the result is a deterministic function of the files — an
    unchanged tree renders a byte-identical prompt prefix, so the provider's cache
    still matches.
    """
    start = Path(agent_dir) if agent_dir is not None else Path.cwd()
    agent_root = _agent_root(start)

    all_skills: dict[str, Skill] = {}
    user_root = _user_skills_root()
    if user_root.is_dir():
        _skills_under(user_root, "user", all_skills)

    agent_skills_root = agent_root / ".emrg" / "skills"
    if agent_skills_root.is_dir():
        _skills_under(agent_skills_root, "agent", all_skills)

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
