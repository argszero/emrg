"""Skill loader — parse Markdown skills with YAML frontmatter.

Skills are filesystem-based Markdown packages. They are read from four roots, **lowest
precedence first**, because that is where the ecosystem actually installs them:

  1. ~/.claude/skills       (label "claude")
  2. ~/.agents/skills       (label "agents")
  3. ~/.emrg/skills         (label "user", from `config_dir()`)
  4. <agent>/.emrg/skills   (label "agent")

A later root's declaration of a name replaces an earlier one's, so an agent's own copy is
the one in effect and the host's instance copy beats both provider directories. A root the
host does not have is skipped without disturbing the others. The list itself is
`default_skill_roots()`, and every reader takes it as a parameter defaulting to that list —
so a test states the roots it means instead of inheriting whatever is installed on the
machine running it.

Each root is read in **both** layouts, `<name>.md` and `<name>/SKILL.md`
(`_skill_files_in`), and the bundle wins over a flat file of the same name inside one root.
`<agent>` is the nearest ancestor of the *session's* working directory carrying a `.git`
(`_agent_root`) — not the process's cwd, which for the daemon is `~/.emrg` and would give
every session one agent's skills.

Nothing here is cached: every `load_skills` call re-reads the files, so a skill
written, edited or deleted on disk reaches the next prompt without a restart.

Each skill file has YAML frontmatter:
  ---
  name: skill-name
  description: What this skill does
  ---
  # Markdown body with instructions

The loader uses a minimal hand-written frontmatter parser to avoid
adding a YAML dependency for the simple key-value format. It reads quoted and plain
values and the block scalars `>`/`>-`/`|`/`|-` (and their `+` forms), because the skills
published into the provider directories write `description:` that way.

Progressive disclosure: only the skill name + description are injected
into the system prompt. The LLM uses the read tool to load the full
body when it decides a skill is relevant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

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
    source: str         # the root it came from: "claude", "agents", "user" or "agent"


@dataclass
class SkillShadow:
    """One declaration of a name that another declaration had already taken.

    ``kept`` is the declaration that survived *every* root, not whichever one happened to
    overtake this loser: a name carried by `~/.claude`, `~/.agents` and `~/.emrg` yields two
    shadows, and both point at `~/.emrg`'s copy — the one the prompt's list above them
    offers, and so the one a reader should edit.

    The two layers of this loader key a skill by different things: `_skill_files_in`
    selects candidate *files* by filename, while `_skills_under` files the parsed
    result under the **declared** `name:`. Two files that declare one name therefore
    meet only in the second layer, and the one that arrives later replaces the one
    before it — within a root "later" means "sorts later by filename", which nothing
    here ever promised, and across roots it is the documented rule that the agent's
    copy beats the user's. Either way a declaration stops being offered, so the loss
    is recorded here rather than left to a `logger.debug` line the deployment discards
    (`stderr=DEVNULL`), which is the shape of a report saying "clean" about a tree it
    could not read.
    """
    name: str           # the declared name both skills claim
    kept: Skill         # the declaration that is offered
    dropped: Skill      # the declaration that lost the name


# Every indicator a YAML block scalar can carry. The set is closed on purpose: a value that
# is one of these opens a block, and anything else is the value itself, so a description
# reading `>` is the one shape that cannot be told from its own text without this list.
_BLOCK_SCALARS = ("|", "|-", "|+", ">", ">-", ">+")


def _join_block_scalar(indicator: str, lines: list[str]) -> str:
    """The real text under a block-scalar key, from the indented lines that follow it.

    Two shapes, and the difference is what the indicator is for: ``|`` is literal and keeps
    its line breaks, ``>`` is folded and joins each run of lines into one paragraph with a
    blank line between paragraphs. A trailing ``-`` strips the final newline and ``+``
    keeps the trailing blank lines; the bare forms behave like ``-`` here, because this text
    is a description rather than a file and a trailing newline in it would be noise in a
    one-line prompt entry.

    The indentation removed is the block's own, taken from its least-indented non-blank
    line, so a block indented by two spaces and one indented by four read alike.
    """
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    if not indicator.endswith("+"):
        while body and not body[-1].strip():
            body.pop()
    indents = [len(line) - len(line.lstrip()) for line in body if line.strip()]
    cut = min(indents) if indents else 0
    stripped = [line[cut:] if line.strip() else "" for line in body]

    if indicator.startswith(">"):
        paragraphs: list[str] = []
        current: list[str] = []
        for line in stripped:
            if line.strip():
                current.append(line.strip())
            elif current:
                paragraphs.append(" ".join(current))
                current = []
        if current:
            paragraphs.append(" ".join(current))
        return "\n".join(paragraphs)
    return "\n".join(stripped).rstrip("\n")


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Parse simple key: value YAML frontmatter.

    Handles quoted strings, plain values and block scalars (``>``, ``>-``, ``|``, ``|-``
    and their ``+`` forms). No nested structures: an indented line outside a block scalar is
    nested YAML and is ignored, so a skill whose frontmatter also carries a nested metadata
    list (skill-catalog.md, rant 2026-08-08T10:14:29) keeps its own top-level
    name/description — nested ``description:`` keys must not overwrite the top-level one.

    Block scalars are read because that is how the skills published into the provider
    directories write their description (rant 2026-10-30T10:42:59): the earlier reader
    stored the indicator as the value and skipped every line of the real text, so such a
    skill was described by the two characters ``>-``.
    """
    result: dict[str, str] = {}
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        if raw[:1].isspace():  # indented → nested YAML, or a block already consumed
            continue
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value in _BLOCK_SCALARS:
            block: list[str] = []
            while index < len(lines):
                following = lines[index]
                if following.strip() and not following[:1].isspace():
                    break  # the next top-level key ends the block
                block.append(following)
                index += 1
            value = _join_block_scalar(value, block)
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
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

    A session is normally opened *inside* an agent rather than at its top, while the
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


# The label each root reports under. The three default roots keep *separate* labels
# because the prompt has to say which one a skill came from, and "user" alone cannot tell a
# `~/.claude/skills` entry from a `~/.emrg/skills` one (rant 2026-10-30T10:42:59, item 7).
ROOT_CLAUDE = "claude"
ROOT_AGENTS = "agents"
ROOT_USER = "user"
ROOT_AGENT = "agent"


def default_skill_roots() -> list[tuple[str, Path]]:
    """The three instance-wide roots and their labels, **lowest precedence first**.

    They hang off ``config_dir()``'s parent rather than a second ``Path.home()`` call, and
    that is load-bearing rather than tidy: `config_dir()` *is* ``~/.emrg``, so its parent is
    the home the other two sit in — one derivation, so a host that redirects the instance
    root redirects all three at once. That is what keeps a suite honest about the fourth
    argument of this rule: measured on the development host, `~/.claude/skills` holds 28
    skills and `~/.agents/skills` 27 of the same names, so a test that inherited them would
    answer one way here and another way on a runner with none.

    The agent's own root is deliberately *not* in this list: it depends on the session, and
    `load_skills_with_shadows` reads it last so that it beats all three.
    """
    home = config_dir().parent
    return [
        (ROOT_CLAUDE, home / ".claude" / "skills"),
        (ROOT_AGENTS, home / ".agents" / "skills"),
        (ROOT_USER, _user_skills_root()),
    ]


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


def _skills_under(
    root: Path, source: str, into: dict[str, Skill], dropped: dict[str, list[Skill]]
) -> None:
    """Read one root into ``into``, a later declaration of a name replacing an earlier one.

    Every declaration that loses its name is appended to ``dropped`` under that name —
    deliberately *not* paired with the declaration that beat it at that moment. With two
    roots the two shapes agree; with three they do not. Reading `~/.claude` → `~/.agents` →
    `~/.emrg`, a name carried by all three is displaced twice, and a per-step pairing
    reports `~/.agents`' copy as "in effect" while the list above the paragraph offers
    `~/.emrg`'s — one prompt contradicting itself about which file a reader should edit.
    `load_skills_with_shadows` therefore pairs each loss with the declaration that actually
    survived, once every root has been read.
    """
    for skill_file in _skill_files_in(root):
        skill = _parse_skill_file(skill_file, source)
        if skill:
            previous = into.get(skill.name)
            if previous is not None:
                dropped.setdefault(skill.name, []).append(previous)
            into[skill.name] = skill
            logger.debug("skill loaded: %s (%s) from %s", skill.name, source, skill_file)


def load_skills_with_shadows(
    agent_dir: Optional[Path] = None,
    roots: Optional[Sequence[tuple[str, Path]]] = None,
) -> tuple[list[Skill], list[SkillShadow]]:
    """Load every skill, plus every declaration that lost its name to another.

    The roots are read **in the order given**, and a later root's declaration of a name
    replaces an earlier one's — which is why the list is ordered lowest precedence first.
    `default_skill_roots()` puts `~/.claude/skills` before `~/.agents/skills` before
    `~/.emrg/skills`, and the agent's own directory is read last whatever the list says, so
    the agent always beats the three instance-wide roots. A root that is absent is skipped
    and the rest are unaffected.

    ``roots`` is how a caller — and above all a test — states the chain it means. Its
    default is the real one; passing a list means the host's own directories are never
    opened, which is the difference between measuring this rule and measuring the machine.

    The merge's discarded half is returned rather than dropped: only a caller that renders
    a prompt has to think about the second list, and everything that just asks "which skills
    are there" keeps calling `load_skills`.
    """
    start = Path(agent_dir) if agent_dir is not None else Path.cwd()
    agent_root = _agent_root(start)

    all_skills: dict[str, Skill] = {}
    displaced: dict[str, list[Skill]] = {}
    for label, root in default_skill_roots() if roots is None else roots:
        root_path = Path(root)
        if root_path.is_dir():
            _skills_under(root_path, label, all_skills, displaced)

    agent_skills_root = agent_root / ".emrg" / "skills"
    if agent_skills_root.is_dir():
        _skills_under(agent_skills_root, ROOT_AGENT, all_skills, displaced)

    # Paired here rather than at each merge step: a name carried by three roots loses two
    # declarations, and both were beaten by the same survivor, not by each other.
    shadows = [
        SkillShadow(name=name, kept=all_skills[name], dropped=loser)
        for name, losers in displaced.items()
        for loser in losers
    ]
    return sorted(all_skills.values(), key=lambda s: s.name), shadows


def load_skills(
    agent_dir: Optional[Path] = None,
    roots: Optional[Sequence[tuple[str, Path]]] = None,
) -> list[Skill]:
    """Load every skill from the three instance roots and one agent root, read live.

    The instance roots are `default_skill_roots()` — ``~/.claude/skills``, then
    ``~/.agents/skills``, then ``~/.emrg/skills`` — and the agent's
    (``<root>/.emrg/skills/``) is read last, so of two skills sharing a name the later
    root's wins: an agent copy beats the instance copy, which beats both provider
    directories. ``roots`` supplies a different chain, which is how a test avoids reading
    the host's own directories.

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

    What a name collision discards is not returned here; `load_skills_with_shadows`
    answers that, and a prompt-reading caller wants it (issue #2563).
    """
    return load_skills_with_shadows(agent_dir)[0]


def build_skills_context(
    skills: list[Skill], shadows: Optional[list[SkillShadow]] = None
) -> str:
    """Build the skill summary text for the system prompt.

    Only includes name + description — the full body is loaded
    on-demand by the LLM using the read tool (progressive disclosure).

    ``shadows`` are the declarations a name collision discarded. They are rendered
    rather than swallowed: a skill that is absent from this list has to have a reason
    a reader can find, and the one trace the loader used to leave is a `logger.debug`
    line whose stream this deployment throws away. The same wording is rendered by
    `system.j2` at the other carrier of this rule — a test compares the two.
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

    if shadows:
        lines.append("")
        lines.append(
            f"**Shadowed names.** {len(shadows)} declaration(s) name a skill that "
            "another declaration already names, so they are not offered under it. The "
            "file in effect is listed above; the declaration that lost the name is named "
            "here, because a skill missing from this list must have a reason rather than "
            "silence."
        )
        for shadow in shadows:
            lines.append(
                f"- **{shadow.name}**: in effect - {shadow.kept.source}, "
                f"`{shadow.kept.path}`; shadowed - {shadow.dropped.source}, "
                f"`{shadow.dropped.path}`"
            )

    return "\n".join(lines)
