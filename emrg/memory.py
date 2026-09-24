"""Memory system for EMRG — persistent LLM knowledge across sessions.

File-based YAML frontmatter + Markdown format, compatible with Claude Code.
Two scopes: project (.emrg/memory/) and session (<session>/memory/).

Usage:
    from emrg.memory import ProjectMemoryStore, SessionMemoryStore, MemoryFile

    # Project-level memories (shared across all sessions)
    pstore = ProjectMemoryStore(cwd)

    # Session-level memories (per-session)
    sstore = SessionMemoryStore(session_dir)

    # CRUD
    mem = pstore.create("decision", "Use httpx", "**Why**: ...")
    mem = pstore.get("a1b2c3d4")
    pstore.update("a1b2c3d4", body="updated body")
    pstore.delete("a1b2c3d4")  # soft-delete (status → superseded)
"""

from __future__ import annotations

import logging
import re
import secrets
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Optional

logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────


def generate_id() -> str:
    """Generate an 8-char random hex id."""
    return secrets.token_hex(4)


def slugify(title: str) -> str:
    """Convert a title to a filename-friendly slug."""
    slug = title.lower().strip()
    # Remove everything except word chars, spaces, and hyphens
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug.strip("-") or "memory"


def now_iso() -> str:
    """Return current local time as ISO 8601 string."""
    return datetime.now().isoformat()


def _short_date(iso_str: str) -> str:
    """Format ISO 8601 to YYYY-MM-DD for display in index."""
    if not iso_str:
        return ""
    # Extract date portion (before T or space)
    return iso_str[:10]


def _truncate_index_title(title: str, max_len: int | None = None) -> str:
    """Truncate an index title to keep MEMORY.md lines bounded.

    The filename (detail file path) is rendered separately in the index line,
    so the full content stays reachable via ``read`` even after truncation.
    """
    if max_len is None:
        max_len = INDEX_TITLE_MAX_CHARS
    if len(title) <= max_len:
        return title
    return title[: max_len - 1] + "…"


# A frontmatter line that declares a top-level key. Indented lines (a nested
# value or a continuation) and comments deliberately do not match: they are not
# keys, so a save must carry them through untouched.
_FM_LINE_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


def _fm_line_key(line: str) -> str | None:
    """The top-level frontmatter key a raw line declares, or None."""
    m = _FM_LINE_KEY_RE.match(line)
    return m.group(1) if m else None


def _fm_line_owners(lines: list[str] | tuple[str, ...]) -> list[str | None]:
    """Which key each frontmatter line belongs to, including its continuations.

    A save replaces a key's line when the store changed that key, so it has to
    know *all* the lines that line owns — otherwise the pieces it left behind
    are the story: a block scalar's continuation lines turn a single-line
    replacement into broken YAML, and a repeated key replayed verbatim after the
    replacement is the one YAML keeps, so the write lands and then reads back as
    the old value. Blank lines and column-0 comments own nothing.
    """
    owners: list[str | None] = []
    current: str | None = None
    for line in lines:
        key = _fm_line_key(line)
        if key is not None:
            current = key
            owners.append(key)
        elif line[:1].isspace():
            owners.append(current)  # an indented continuation of the key above
        else:
            current = None
            owners.append(None)
    return owners


# The keys `MemoryFile` owns — the ones it renders, and so the only ones it may
# replace or drop. A key outside this set belongs to the file, not to the model.
_OWNED_FIELDS = frozenset(
    {
        "id",
        "event_at",
        "created_at",
        "updated_at",
        "source_session",
        "type",
        "scope",
        "status",
    }
)


# ── Constants ──────────────────────────────────────────────────────

VALID_TYPES = {"user", "feedback", "project", "reference", "decision", "task"}
VALID_SCOPES = {"session", "project"}
VALID_STATUSES = {"active", "superseded", "merged"}

# Rant 2026-08-23T08:04:26 — memory index governance: keep MEMORY.md lines
# bounded so the embedded index can't bloat the system prompt (413 / cost /
# cache invalidation). Write-time truncation is primary; render-time
# truncation is a fallback for legacy dirty data. Consolidation is
# LLM-driven; these are soft guards (warn/truncate, never auto-delete).
INDEX_TITLE_MAX_CHARS = 512  # max chars for one index title / line
INDEX_COUNT_WARN = 100       # >N memory files → consolidation recommended
# >50KB MEMORY.md → consolidation recommended. One number, two *units*, which is
# why neither site may assume the other's reading: the two advisory readers compare
# it against the file's **byte** size (`SessionMemoryStore._warn_index_thresholds`
# below, and the reflection prompt's hygiene note, which prints the reading as
# "N bytes"), while `EmrgServer._cap_memory_index` applies it as a **character**
# budget, because what it bounds is a prompt and prompts are counted in characters
# (the incident the cap came from is quoted that way: 77% of a 452,972-char prompt).
# The two disagree about one CJK index — measured 2026-09-24, a 30,024-char /
# 61,160-byte index fires the advisory while the cap truncates nothing, and the band
# where that happens is wide, since CJK runs ~3 bytes per character. The direction
# that matters holds structurally rather than by luck: the cap can only truncate an
# index the advisory has already flagged, never one it stayed silent about, because
# UTF-8 never encodes a string in fewer bytes than characters. Mechanised in
# `tests/test_memory_index_thresholds.py`.
INDEX_SIZE_WARN = 50 * 1024

# Order of the `## type` sections when the index has to be rendered from
# entries alone (a rebuild, or entries the document never had). The parser
# accepts any `## <valid type>` heading; this only decides where new rows file.
INDEX_TYPE_ORDER = ("user", "feedback", "project", "reference", "decision", "task")

# ── MemoryFile ─────────────────────────────────────────────────────


@dataclass
class MemoryFile:
    """A single memory: YAML frontmatter + Markdown body.

    Fields match the design in .emrg/memory-design.md §3.2.
    """

    id: str = field(default_factory=generate_id)
    event_at: str = field(default_factory=now_iso)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    source_session: Optional[str] = None
    type: str = "reference"
    scope: str = "session"
    status: str = "active"
    title: str = ""
    body: str = ""

    # Provenance, not content: the frontmatter block as it was read, and the
    # value each key parsed to. ``to_markdown`` replays the source lines for
    # values the store did not change, so a load → save of a file the store
    # merely read reproduces that file. ClassVars so they stay out of the
    # dataclass's fields, equality and constructor.
    _fm_lines: ClassVar[tuple[str, ...] | None] = None
    _fm_parsed: ClassVar[dict[str, object]] = {}

    @property
    def filename(self) -> str:
        """Derive a descriptive filename from title.

        Does NOT include the id — the id lives in frontmatter only.
        This keeps filenames human-readable.
        """
        prefix = f"{self.type}-" if self.type != "reference" else ""
        slug = slugify(self.title)
        return f"{prefix}{slug}.md"

    # ── Parsing ────────────────────────────────────────────────

    @classmethod
    def from_file(cls, path: Path) -> MemoryFile:
        """Parse a memory file from disk."""
        if not path.exists():
            raise FileNotFoundError(f"Memory file not found: {path}")
        content = path.read_text(encoding="utf-8")
        return cls.from_text(content, _filename=path.name)

    @classmethod
    def from_text(cls, text: str, _filename: str = "") -> MemoryFile:
        """Parse memory from markdown text with YAML frontmatter.

        Frontmatter is delimited by --- on its own lines at the start.
        Uses yaml.safe_load with fallback to hand-written parser for invalid
        YAML (e.g. LLM-generated files with syntax errors).
        """
        frontmatter: dict = {}
        fm_lines: tuple[str, ...] | None = None
        body = ""
        lines = text.split("\n")

        if lines and lines[0].strip() == "---":
            # Find closing ---
            end_idx = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    end_idx = i
                    break

            if end_idx is not None:
                fm_raw = "\n".join(lines[1:end_idx])
                fm_lines = tuple(lines[1:end_idx])
                body = "\n".join(lines[end_idx + 1 :]).strip()

                # Try yaml.safe_load first (handles lists, nested values, etc.)
                try:
                    parsed = yaml.safe_load(fm_raw)
                    if isinstance(parsed, dict):
                        # Convert all values to string (yaml may produce int, list, None, etc.)
                        for key, value in parsed.items():
                            if value is None:
                                frontmatter[key] = None
                            elif isinstance(value, list):
                                frontmatter[key] = " ".join(str(v) for v in value)
                            elif isinstance(value, bool):
                                frontmatter[key] = "true" if value else "false"
                            elif isinstance(value, datetime):
                                # An unquoted ISO timestamp is a datetime to
                                # yaml, and `str()` would hand back the same
                                # instant in a different format ("2026-08-23
                                # 12:27:14+08:00"). This module's own field
                                # docs — and the memory format the system
                                # prompt gives agents — say ISO 8601, so a
                                # value read from such a file must stay ISO.
                                frontmatter[key] = value.isoformat()
                            else:
                                frontmatter[key] = str(value)
                except (yaml.YAMLError, yaml.MarkedYAMLError):
                    # Fallback: hand-written parser for malformed YAML
                    current_key = None
                    for line in lines[1:end_idx]:
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#"):
                            continue

                        # Handle multi-line values (indented continuation)
                        if line.startswith(" ") and current_key:
                            value_part = stripped
                            if value_part:
                                existing = frontmatter.get(current_key)
                                if existing is None:
                                    existing = ""
                                frontmatter[current_key] = existing + " " + value_part
                            continue

                        # Simple key: value
                        match = re.match(r"^(\w[\w_]*)\s*:\s*(.*)", stripped)
                        if match:
                            current_key = match.group(1)
                            value = match.group(2).strip()

                            # Strip surrounding quotes
                            if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                                value = value[1:-1]
                            # Handle null sentinels
                            if value in ("", "null", "~", "None"):
                                frontmatter[current_key] = None
                            else:
                                frontmatter[current_key] = value
        else:
            body = text.strip()

        # Extract title from first heading or filename
        title = ""
        body_first_line = body.split("\n")[0] if body else ""
        heading_match = re.match(r"^#\s+(.+)", body_first_line)
        if heading_match:
            title = heading_match.group(1).strip()
        elif _filename:
            # Derive from filename: "decision-use-httpx.md" → "Decision: use httpx"
            stem = Path(_filename).stem
            # Remove type prefix
            for t in sorted(VALID_TYPES, key=len, reverse=True):
                if stem.startswith(f"{t}-"):
                    stem = stem[len(t) + 1 :]
                    break
            title = stem.replace("-", " ")

        mem = cls(
            id=frontmatter.get("id") or generate_id(),
            event_at=frontmatter.get("event_at") or now_iso(),
            created_at=frontmatter.get("created_at") or now_iso(),
            updated_at=frontmatter.get("updated_at") or now_iso(),
            source_session=frontmatter.get("source_session"),
            type=frontmatter.get("type") or "reference",
            scope=frontmatter.get("scope") or "session",
            status=frontmatter.get("status") or "active",
            title=title,
            body=body,
        )
        # Provenance for the write side: the lines as read, and the value each
        # key parsed to, so ``to_markdown`` can tell what it changed.
        mem._fm_lines = fm_lines
        mem._fm_parsed = dict(frontmatter)
        return mem

    # ── Serialization ──────────────────────────────────────────

    def to_markdown(self) -> str:
        """Serialize to the full file format (frontmatter + body).

        The frontmatter is written as a document, not as a re-rendering of this
        model's eight fields. Lines the store did not change come out of
        ``_fm_lines`` verbatim — including keys this model does not know (they
        used to be dropped outright) and values yaml coerced into something
        else. Only keys the file never had, and values the store actually
        changed, are rendered canonically. Measured 2026-09-14 over the 1216
        ``.md`` files under ``~/.emrg`` memory directories: 1104 came back
        different from a load → save (an ISO ``T`` became a space, ``Z`` became
        ``+00:00``, every value gained quotes), which is the shape the memory
        format spec tells agents to write — so every hand-written file was
        rewritten by the next store write. With this, 1186 are byte-exact; the
        30 that still differ are 21 index/archive files that carry no
        frontmatter at all (``MemoryIndex`` writes those, not this class), 8
        whose body ends in a blank line, and 1 missing the required timestamps.

        Replaying a line is only safe if the save knows every line that line
        owns. A changed key is replaced *with* its continuations (otherwise a
        block scalar's remaining lines re-attach to a single-line replacement
        and the frontmatter stops parsing) and *at every occurrence* (otherwise
        a repeated key is still there verbatim, and YAML keeps the last one —
        so the store's write lands and then reads back as the old value).
        Neither shape occurs in this machine's corpus (0 of 1654 frontmatter
        files, measured 2026-09-14), but both were one write away from silent
        data loss, and a writer that only works on the shapes already on disk
        is a writer that fails on the shapes a person types next.
        """
        canonical = self._canonical_frontmatter()
        lines = list(self._fm_lines or ())
        owners = _fm_line_owners(lines)
        # A key is replaced when the file's line for it no longer says what this
        # model says: the value changed, or the field now renders to nothing (a
        # cleared `source_session` has to go, not sit there contradicting it).
        replaced: set[str] = set()
        for key in set(owners):
            if key is None or key not in _OWNED_FIELDS or key not in self._fm_parsed:
                continue  # not ours, or new — a new field is appended below
            if (
                canonical.get(key) is not None
                and self._fm_parsed[key] == getattr(self, key)
            ):
                continue  # untouched since it was read: replay it verbatim
            replaced.add(key)

        out = ["---"]
        emitted: set[str] = set()
        for line, owner in zip(lines, owners, strict=True):
            if owner is None or owner not in _OWNED_FIELDS:
                out.append(line)  # a comment, a blank, or a key that is not ours
                continue
            if owner not in replaced:
                emitted.add(owner)  # this field did not move: replay as written
                out.append(line)
                continue
            if owner in emitted:
                continue  # its canonical line already stands for the key
            emitted.add(owner)
            if canonical.get(owner) is not None:
                out.append(canonical[owner])
        for key, line in canonical.items():
            if key not in emitted:
                out.append(line)
        out.append("---")
        out.append("")

        body = self.body
        # Ensure title heading exists
        if self.title:
            title_line = f"# {self.title}"
            if not body.startswith(title_line):
                body = f"{title_line}\n\n{body}"
        out.append(body)

        # Trailing newline
        return "\n".join(out) + "\n"

    def _canonical_frontmatter(self) -> dict[str, str]:
        """How this model renders each field it owns, in its own order."""
        fm = {
            "id": f'id: "{self.id}"',
            "event_at": f'event_at: "{self.event_at}"',
            "created_at": f'created_at: "{self.created_at}"',
            "updated_at": f'updated_at: "{self.updated_at}"',
        }
        if self.source_session:
            fm["source_session"] = f'source_session: "{self.source_session}"'
        fm["type"] = f'type: "{self.type}"'
        fm["scope"] = f'scope: "{self.scope}"'
        fm["status"] = f'status: "{self.status}"'
        return fm

    def save(self, path: Path) -> None:
        """Write this memory file to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_markdown(), encoding="utf-8")

    # ── Display ────────────────────────────────────────────────

    @property
    def display_title(self) -> str:
        """Human-friendly title for UI/index display."""
        return self.title or self.filename

    @property
    def event_short(self) -> str:
        """Short date for the event this memory describes."""
        return _short_date(self.event_at)

    @property
    def created_short(self) -> str:
        """Short date for when this was recorded."""
        return _short_date(self.created_at)


# ── MemoryIndex ────────────────────────────────────────────────────


@dataclass
class _IndexEntry:
    """Internal: one entry in the MEMORY.md index."""

    title: str = ""
    filename: str = ""
    type: str = "reference"
    status: str = "active"
    created_at: str = ""
    event_at: str = ""
    updated_at: str = ""
    # The index line this entry was parsed from, byte for byte. Kept so a
    # hand-written index survives a load → save round trip: the renderer emits
    # the source line verbatim unless the store itself rewrote the entry.
    # Empty for entries the store created (they have no source line).
    raw: str = ""


class MemoryIndex:
    """Manages a MEMORY.md index file.

    Format (compatible with Claude Code)::

        # Memory Index

        ## user
        - [User prefers Chinese](user-pref-language.md) — rec: 2026-07-14, evt: 2026-07-10

        ## decision
        - [Use httpx](decision-use-httpx.md) [superseded] — rec: 2026-07-14, evt: 2026-07-03

    Round-trip fidelity: MEMORY.md is a file agents hand-edit (they append rows
    directly), so it holds more than this model represents — a document title,
    ``>`` notes, per-row prose, a row listing several links. ``from_text``
    therefore keeps the source document and the line each entry came from, and
    ``to_markdown`` walks that document instead of re-rendering it from parsed
    fields: lines it does not understand come out verbatim, an entry line comes
    out verbatim unless the store rewrote that entry, and only genuinely new
    entries are appended under their ``## type`` heading. Loading an untouched
    index and saving it back returns the same text (normalised to one trailing
    newline) — no row, note or heading is lost.
    """

    def __init__(self, entries: list[_IndexEntry] | None = None):
        self.entries: list[_IndexEntry] = entries or []
        # Document skeleton: the index file's own lines in order, and the
        # filename each entry line parsed to. A fresh index (nothing loaded)
        # starts from just the title, so every entry counts as new.
        self._lines: list[str] = ["# Memory Index", ""]
        self._src: dict[int, str] = {}

    # ── Mutation ───────────────────────────────────────────────

    def add_entry(self, mem: MemoryFile) -> None:
        """Add or update a memory entry.

        Identifies entries by filename (since id is not in the index).
        """
        # Remove existing entry with same filename
        self.entries = [e for e in self.entries if e.filename != mem.filename]

        # Rant 2026-08-23T08:04:26 — write-time truncation (primary guard):
        # keep the stored index title bounded; the full title lives in the
        # standalone .md frontmatter, and the filename stays in the index line.
        title = _truncate_index_title(mem.display_title)

        self.entries.append(
            _IndexEntry(
                title=title,
                filename=mem.filename,
                type=mem.type,
                status=mem.status,
                created_at=mem.created_at,
                event_at=mem.event_at,
                updated_at=mem.updated_at,
            )
        )
        # Sort by updated_at descending (recent first)
        self.entries.sort(key=lambda e: e.updated_at or "", reverse=True)

    def remove_entry(self, filename: str) -> None:
        """Remove an entry by filename."""
        self.entries = [e for e in self.entries if e.filename != filename]

    # ── Rendering ──────────────────────────────────────────────

    def to_markdown(self) -> str:
        """Render the index, preserving everything this model cannot hold.

        Walks the source document (``_lines``): non-entry lines are emitted as
        written, and an entry line is emitted verbatim unless the store rewrote
        that entry (``raw`` empty) or the line exceeds the index line cap.
        Entries the document never had are appended under their ``## type``
        heading. Re-rendering every line from parsed fields — what this used to
        do — dropped a document title, ``>`` notes, per-row prose and any row
        listing several links, i.e. any load → save destroyed the hand-written
        index: measured on this repo's own indexes, 22389 chars came back as
        2628 and 2388 chars as 349.
        """
        live = {e.filename: e for e in self.entries}
        out: list[str] = []
        placed: set[str] = set()

        for i, line in enumerate(self._lines):
            filename = self._src.get(i)
            if filename is None:
                out.append(line)  # title, `>` notes, headings, prose — as written
                continue
            entry = live.get(filename)
            if entry is None:
                continue  # the store removed this entry
            if filename in placed and not entry.raw:
                continue  # a rewritten entry renders once, however many rows it had
            placed.add(filename)
            if entry.raw and len(entry.raw) <= INDEX_TITLE_MAX_CHARS:
                out.append(entry.raw)
            else:
                out.append(self._render_entry(entry))

        fresh = [e for e in self.entries if e.filename not in placed]
        # Leftover types (not in INDEX_TYPE_ORDER) are filed last rather than
        # dropped — an unknown type is a rendering question, not data loss.
        leftovers = sorted({e.type for e in fresh} - set(INDEX_TYPE_ORDER))
        for t in INDEX_TYPE_ORDER + tuple(leftovers):
            group = [e for e in fresh if e.type == t]
            if group:
                self._append_under(out, t, [self._render_entry(e) for e in group])

        return "\n".join(out).strip() + "\n"

    @staticmethod
    def _render_entry(e: _IndexEntry) -> str:
        """One entry as a fresh index line (bounded by INDEX_TITLE_MAX_CHARS)."""
        status_tag = f" [{e.status}]" if e.status != "active" else ""
        rec = f"rec: {_short_date(e.created_at)}" if e.created_at else ""
        evt = f"evt: {_short_date(e.event_at)}" if e.event_at else ""
        date_part = ", ".join(p for p in [rec, evt] if p)
        line = f"- [{e.title}]({e.filename}){status_tag} — {date_part}"
        if len(line) <= INDEX_TITLE_MAX_CHARS:
            return line
        # Rant 2026-08-23T08:04:26 — render-time fallback for legacy dirty
        # index data (write-time truncation wasn't in place). Keep the filename
        # so the detail file stays reachable.
        logger.warning(
            "memory index line exceeds %d chars (title=%d chars) — truncating",
            INDEX_TITLE_MAX_CHARS, len(e.title),
        )
        other = len(f"- []({e.filename}){status_tag} — {date_part}")
        budget = max(1, INDEX_TITLE_MAX_CHARS - other)
        return (
            f"- [{_truncate_index_title(e.title, budget)}]"
            f"({e.filename}){status_tag} — {date_part}"
        )

    @staticmethod
    def _append_under(out: list[str], type_name: str, lines: list[str]) -> None:
        """Insert rendered entry lines into ``out`` under `## type_name`."""
        heading = f"## {type_name}"
        if heading not in out:
            if out and out[-1].strip():
                out.append("")
            out.extend([heading, *lines, ""])
            return
        start = out.index(heading)
        end = len(out)
        for j in range(start + 1, len(out)):
            if out[j].startswith("## "):
                end = j
                break
        while end > start + 1 and not out[end - 1].strip():
            end -= 1  # keep the section's trailing blank line
        out[end:end] = lines

    def save(self, path: Path) -> None:
        """Write the index to disk."""
        path.parent.mkdir(parents=True, exist_ok=True)
        old_size = path.stat().st_size if path.exists() else 0
        content = self.to_markdown()
        path.write_text(content, encoding="utf-8")
        new_size = len(content.encode("utf-8"))
        logger.info(
            "memory index saved: %s (%d entries, %d → %d bytes)",
            path, len(self.entries), old_size, new_size,
        )

    # ── Parsing ────────────────────────────────────────────────

    @classmethod
    def from_file(cls, path: Path) -> MemoryIndex:
        """Parse an existing MEMORY.md file (returns empty index if missing)."""
        if not path.exists():
            return cls()
        return cls.from_text(path.read_text(encoding="utf-8"))

    @classmethod
    def from_text(cls, text: str) -> MemoryIndex:
        """Parse MEMORY.md content into entries, keeping the document itself."""
        idx = cls()
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()  # a trailing newline is not a line of its own
        if lines:
            idx._lines = lines
        entries: list[_IndexEntry] = []
        current_type = "reference"

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Detect type heading: ## user
            tm = re.match(r"^##\s+(\w+)", stripped)
            if tm and tm.group(1) in VALID_TYPES:
                current_type = tm.group(1)
                continue

            # Detect entry: - [Title](file.md) [status] — rec: ..., evt: ...
            em = re.match(
                r"^-\s+\[(.+?)\]\((.+?)\)(?:\s+\[(\w+)\])?\s*[-—]\s*(.+)",
                stripped,
            )
            if em:
                title = em.group(1)
                filename = em.group(2)
                status = em.group(3) or "active"
                rest = em.group(4)

                created_at = ""
                event_at = ""
                rec_m = re.search(r"rec:\s*([\d\-T:]+)", rest)
                if rec_m:
                    created_at = _normalize_date(rec_m.group(1))
                evt_m = re.search(r"evt:\s*([\d\-T:]+)", rest)
                if evt_m:
                    event_at = _normalize_date(evt_m.group(1))

                entries.append(
                    _IndexEntry(
                        title=title,
                        filename=filename,
                        type=current_type,
                        status=status,
                        created_at=created_at,
                        event_at=event_at,
                        updated_at=created_at,
                        raw=line,
                    )
                )
                idx._src[i] = filename

        idx.entries = entries
        return idx


def _normalize_date(d: str) -> str:
    """Ensure a date string has time component for ISO 8601."""
    if not d:
        return ""
    d = d.strip()
    if "T" in d:
        return d
    return d + "T00:00:00Z"


# ── MemoryStore ────────────────────────────────────────────────────


class MemoryStore:
    """Manages memory files in a directory with a MEMORY.md index.

    Base class — use :class:`ProjectMemoryStore` or :class:`SessionMemoryStore`.
    """

    def __init__(self, directory: Path, scope: str):
        if scope not in VALID_SCOPES:
            raise ValueError(f"Invalid scope: {scope}. Must be one of {VALID_SCOPES}")
        self.directory = directory
        self.scope = scope
        self.directory.mkdir(parents=True, exist_ok=True)

    @property
    def index_path(self) -> Path:
        return self.directory / "MEMORY.md"

    # ── Index helpers ──────────────────────────────────────────

    def _load_index(self) -> MemoryIndex:
        return MemoryIndex.from_file(self.index_path)

    def _save_index(self, index: MemoryIndex, source: str = "") -> None:
        if source:
            logger.debug("memory index write from: %s", source)
        index.save(self.index_path)

    def _rebuild_index(self) -> MemoryIndex:
        """Rebuild the entire index by scanning all .md files."""
        idx = MemoryIndex()
        for path in sorted(self.directory.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                mem = MemoryFile.from_file(path)
                idx.add_entry(mem)
            except (OSError, ValueError):
                logger.debug("Skipping unparseable memory: %s", path, exc_info=True)
        return idx

    # ── File lookup ────────────────────────────────────────────

    def _find_by_id(self, mem_id: str) -> Path | None:
        """Scan directory for a .md file whose frontmatter id matches."""
        for path in sorted(self.directory.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                mem = MemoryFile.from_file(path)
                if mem.id == mem_id:
                    return path
            except (OSError, ValueError):
                logger.debug("Skipping unparseable memory: %s", path, exc_info=True)
        return None

    def _find_by_filename(self, filename: str) -> Path | None:
        """Find a memory .md file by filename."""
        path = self.directory / filename
        return path if path.exists() else None

    def _resolve_filename(self, mem: MemoryFile) -> str:
        """Return a unique filename, appending a counter if needed."""
        base = mem.filename
        stem = Path(base).stem
        candidate = base
        counter = 1
        while (self.directory / candidate).exists():
            # Check if existing file has the same id (update in place)
            try:
                existing = MemoryFile.from_file(self.directory / candidate)
                if existing.id == mem.id:
                    return candidate  # same memory, overwrite
            except (OSError, ValueError):
                pass
            counter += 1
            candidate = f"{stem}-{counter}.md"
        return candidate

    # ── CRUD ───────────────────────────────────────────────────

    def create(
        self,
        type: str,
        title: str,
        body: str,
        *,
        event_at: str | None = None,
        source_session: str | None = None,
    ) -> MemoryFile:
        """Create a new memory and persist to disk.

        Args:
            type: One of user, feedback, project, reference, decision, task.
            title: Short descriptive title.
            body: Markdown body (What / Why / How to apply for decision types).
            event_at: When the event happened (ISO 8601). Defaults to now.
            source_session: Session that produced this memory.

        Returns:
            The created MemoryFile with auto-generated id and timestamps.
        """
        if type not in VALID_TYPES:
            raise ValueError(f"Invalid type: {type!r}. Must be one of {VALID_TYPES}")

        now = now_iso()
        mem = MemoryFile(
            id=generate_id(),
            event_at=event_at or now,
            created_at=now,
            updated_at=now,
            source_session=source_session,
            type=type,
            scope=self.scope,
            status="active",
            title=title,
            body=body,
        )

        filename = self._resolve_filename(mem)
        filepath = self.directory / filename

        # Re-create with resolved filename (so to_markdown + index match)
        mem = MemoryFile(
            id=mem.id,
            event_at=mem.event_at,
            created_at=mem.created_at,
            updated_at=mem.updated_at,
            source_session=mem.source_session,
            type=mem.type,
            scope=mem.scope,
            status=mem.status,
            title=mem.title,
            body=mem.body,
        )
        # Override filename for this instance
        object.__setattr__(mem, "_filename", filename)
        mem.save(filepath)

        # Update index
        index = self._load_index()
        index.add_entry(mem)
        self._save_index(index, "create")

        logger.info(
            "memory created: id=%s type=%s title=%r in %s (%d total entries)",
            mem.id,
            mem.type,
            mem.title,
            self.directory,
            len(index.entries),
        )
        return mem

    def get(self, mem_id: str) -> MemoryFile | None:
        """Get a memory by its frontmatter id."""
        path = self._find_by_id(mem_id)
        if path is None:
            return None
        return MemoryFile.from_file(path)

    def get_by_filename(self, filename: str) -> MemoryFile | None:
        """Get a memory by its index filename."""
        path = self._find_by_filename(filename)
        if path is None:
            return None
        return MemoryFile.from_file(path)

    def list(
        self,
        type_filter: str | None = None,
        status_filter: str | None = None,
    ) -> list[MemoryFile]:
        """List all memories, optionally filtered by type or status."""
        memories: list[MemoryFile] = []
        for path in sorted(self.directory.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                mem = MemoryFile.from_file(path)
                if type_filter and mem.type != type_filter:
                    continue
                if status_filter and mem.status != status_filter:
                    continue
                memories.append(mem)
            except (OSError, ValueError):
                logger.debug("Skipping unparseable memory: %s", path, exc_info=True)
        return memories

    def update(self, mem_id: str, **kwargs) -> MemoryFile | None:
        """Update fields of an existing memory.

        Allowed fields: title, body, type, status, event_at, source_session.
        Automatically bumps updated_at.
        """
        path = self._find_by_id(mem_id)
        if path is None:
            logger.warning("memory not found for update: id=%s", mem_id)
            return None

        mem = MemoryFile.from_file(path)
        allowed = {"title", "body", "type", "status", "event_at", "source_session"}
        changed = False

        for key, value in kwargs.items():
            if key not in allowed:
                logger.debug("skipping disallowed field in update: %s", key)
                continue
            if getattr(mem, key) != value:
                setattr(mem, key, value)
                changed = True

        if changed:
            mem.updated_at = now_iso()

        mem.save(path)

        # Update index
        index = self._load_index()
        index.add_entry(mem)
        self._save_index(index, "update")

        logger.info("memory updated: id=%s title=%r", mem.id, mem.title)
        return mem

    def delete(self, mem_id: str) -> bool:
        """Soft-delete a memory: mark status as 'superseded'.

        The file is preserved; only the status changes.
        Returns True if the memory was found and marked.
        """
        result = self.update(mem_id, status="superseded")
        if result:
            logger.info("memory superseded: id=%s", mem_id)
        return result is not None

    def merge(self, mem_id: str, merged_into_id: str) -> bool:
        """Mark a memory as merged into another (soft-delete variant)."""
        result = self.update(mem_id, status="merged")
        if result:
            # Append a note to the body
            path = self._find_by_id(mem_id)
            if path:
                mem = MemoryFile.from_file(path)
                mem.body += f"\n\n*Merged into memory `{merged_into_id}`.*\n"
                mem.save(path)
            logger.info("memory merged: id=%s → %s", mem_id, merged_into_id)
        return result is not None

    # ── Maintenance ────────────────────────────────────────────

    def rebuild_index(self) -> None:
        """Rebuild MEMORY.md from all .md files on disk.

        Useful if the index gets out of sync or corrupted.
        """
        index = self._rebuild_index()
        self._save_index(index, "rebuild_index")
        logger.info(
            "index rebuilt: %d entries in %s", len(index.entries), self.index_path
        )

    @property
    def count(self) -> int:
        """Number of .md memory files (excluding MEMORY.md)."""
        return len([p for p in self.directory.glob("*.md") if p.name != "MEMORY.md"])

    def promote_to_project(
        self, mem_id: str, project_store: MemoryStore
    ) -> MemoryFile | None:
        """Move a session-scoped memory to the project store.

        The original file is marked as ``merged`` with a .promoted marker.
        The new project-level copy gets ``scope: project`` and is added
        to the project MEMORY.md index.

        Args:
            mem_id: The memory to promote.
            project_store: A ProjectMemoryStore instance.

        Returns:
            The promoted MemoryFile (now in project store), or None if not found.
        """
        path = self._find_by_id(mem_id)
        if path is None:
            logger.warning("memory not found for promotion: id=%s", mem_id)
            return None

        mem = MemoryFile.from_file(path)

        # Create project-level copy
        mem.scope = "project"
        mem.updated_at = now_iso()

        new_path = project_store.directory / mem.filename
        # Handle filename conflicts in project store
        counter = 1
        while new_path.exists():
            stem = Path(mem.filename).stem
            new_path = project_store.directory / f"{stem}-{counter}.md"
            counter += 1

        mem.save(new_path)

        # Update project index
        pindex = project_store._load_index()
        pindex.add_entry(mem)
        project_store._save_index(pindex, "promote_to_project")

        # Mark original as merged + write marker
        mem_original = MemoryFile.from_file(path)
        mem_original.status = "merged"
        mem_original.updated_at = now_iso()
        mem_original.body += (
            f"\n\n*Promoted to project memory: `{new_path}` on {now_iso()}.*\n"
        )
        mem_original.save(path)

        # Create .promoted marker
        marker = path.with_suffix(".promoted")
        marker.write_text(
            f"promoted_at: {now_iso()}\n"
            f"target: {new_path}\n"
            f"memory_id: {mem.id}\n",
            encoding="utf-8",
        )

        # Update session index (remove the entry since it's now merged)
        sindex = self._load_index()
        sindex.remove_entry(path.name)
        self._save_index(sindex, "promote_to_project_remove")

        logger.info("memory promoted to project: id=%s → %s", mem.id, new_path)
        return MemoryFile.from_file(new_path)


# ── Concrete stores ─────────────────────────────────────────────


class ProjectMemoryStore(MemoryStore):
    """Project-level memory: ``<cwd>/.emrg/memory/`` — shared across all sessions."""

    def __init__(self, cwd: Path):
        super().__init__(cwd / ".emrg" / "memory", scope="project")


class SessionMemoryStore(MemoryStore):
    """Session-level memory: ``<session_dir>/memory/`` — per-session."""

    def __init__(self, session_dir: Path):
        super().__init__(session_dir / "memory", scope="session")

    # Rant 2026-08-23T08:04:26 — soft guard only: warn when the session index
    # crosses thresholds. Actual consolidation (merge/trim to ≤50) is
    # LLM-driven via reflection/consolidation prompts — never auto-delete here.
    def _save_index(self, index: MemoryIndex, source: str = "") -> None:
        super()._save_index(index, source)
        self._warn_index_thresholds()

    def _warn_index_thresholds(self) -> None:
        """Log a warning when the session index exceeds soft thresholds."""
        try:
            if self.count > INDEX_COUNT_WARN:
                logger.warning(
                    "memory index ≥ threshold — consolidation recommended: "
                    "count=%d > %d (%s)",
                    self.count, INDEX_COUNT_WARN, self.index_path,
                )
            if self.index_path.exists():
                size = self.index_path.stat().st_size
                if size > INDEX_SIZE_WARN:
                    logger.warning(
                        "memory index ≥ threshold — consolidation recommended: "
                        "size=%d bytes > %d (%s)",
                        size, INDEX_SIZE_WARN, self.index_path,
                    )
        except OSError:
            logger.debug("memory index threshold check skipped", exc_info=True)
