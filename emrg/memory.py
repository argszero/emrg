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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

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


# ── Constants ──────────────────────────────────────────────────────

VALID_TYPES = {"user", "feedback", "project", "reference", "decision", "task"}
VALID_SCOPES = {"session", "project"}
VALID_STATUSES = {"active", "superseded", "merged"}

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
        Keys use simple ``key: value`` syntax (no nested YAML).
        """
        frontmatter: dict = {}
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
                fm_lines = lines[1:end_idx]
                current_key = None
                for line in fm_lines:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue

                    # Handle multi-line values (indented continuation)
                    if line.startswith(" ") and current_key:
                        value_part = stripped
                        if value_part:
                            existing = frontmatter.get(current_key, "")
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

                body = "\n".join(lines[end_idx + 1 :]).strip()
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

        return cls(
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

    # ── Serialization ──────────────────────────────────────────

    def to_markdown(self) -> str:
        """Serialize to the full file format (frontmatter + body)."""
        fm = [
            "---",
            f'id: "{self.id}"',
            f'event_at: "{self.event_at}"',
            f'created_at: "{self.created_at}"',
            f'updated_at: "{self.updated_at}"',
        ]
        if self.source_session:
            fm.append(f'source_session: "{self.source_session}"')
        fm.append(f'type: "{self.type}"')
        fm.append(f'scope: "{self.scope}"')
        fm.append(f'status: "{self.status}"')
        fm.append("---")
        fm.append("")

        body = self.body
        # Ensure title heading exists
        if self.title:
            title_line = f"# {self.title}"
            if not body.startswith(title_line):
                body = f"{title_line}\n\n{body}"
        fm.append(body)

        # Trailing newline
        return "\n".join(fm) + "\n"

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


class MemoryIndex:
    """Manages a MEMORY.md index file.

    Format (compatible with Claude Code)::

        # Memory Index

        ## user
        - [User prefers Chinese](user-pref-language.md) — rec: 2026-07-14, evt: 2026-07-10

        ## decision
        - [Use httpx](decision-use-httpx.md) [superseded] — rec: 2026-07-14, evt: 2026-07-03
    """

    def __init__(self, entries: list[_IndexEntry] | None = None):
        self.entries: list[_IndexEntry] = entries or []

    # ── Mutation ───────────────────────────────────────────────

    def add_entry(self, mem: MemoryFile) -> None:
        """Add or update a memory entry.

        Identifies entries by filename (since id is not in the index).
        """
        # Remove existing entry with same filename
        self.entries = [e for e in self.entries if e.filename != mem.filename]

        self.entries.append(
            _IndexEntry(
                title=mem.display_title,
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
        """Render the full index as markdown."""
        lines = ["# Memory Index", ""]

        # Group by type
        by_type: dict[str, list[_IndexEntry]] = {}
        for e in self.entries:
            by_type.setdefault(e.type, []).append(e)

        # Stable type ordering
        type_order = ["user", "feedback", "project", "reference", "decision", "task"]
        for t in type_order:
            if t not in by_type:
                continue
            lines.append(f"## {t}")
            for e in by_type[t]:
                status_tag = f" [{e.status}]" if e.status != "active" else ""
                rec = f"rec: {_short_date(e.created_at)}" if e.created_at else ""
                evt = f"evt: {_short_date(e.event_at)}" if e.event_at else ""
                date_part = ", ".join(p for p in [rec, evt] if p)
                lines.append(f"- [{e.title}]({e.filename}){status_tag} — {date_part}")
            lines.append("")

        return "\n".join(lines).strip() + "\n"

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
        """Parse MEMORY.md content into entries."""
        entries: list[_IndexEntry] = []
        current_type = "reference"

        for line in text.split("\n"):
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
                    )
                )

        return cls(entries)


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
