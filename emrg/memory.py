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
from typing import ClassVar, NamedTuple, Optional

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


#: The longest a memory's filename may be, counted in **bytes**.
#:
#: Measured 2026-10-01 on this host: `os.pathconf(dir, "PC_NAME_MAX")` answers 255, and
#: that is exactly where the boundary is — a 255-character name is accepted, 256 raises
#: `OSError: [Errno 63] File name too long`. Nothing capped the derived name, so a long
#: title produced a bare OS error out of `_resolve_filename`'s `.exists()` (measured with
#: a 250-character title): the memory could not be written **at all**, and the failure
#: named a temp path rather than the length.
#:
#: Bytes, not characters, because the unit is the platform's: this filesystem counts
#: **characters** (255 CJK characters = 765 bytes are accepted here, measured), while
#: Linux's `NAME_MAX = 255` counts **bytes**. UTF-8 length is never below character
#: count, so capping in bytes is the safe side of both — and `PC_NAME_MAX` being 255
#: under either reading is why one number works for both.
FILENAME_MAX_BYTES = 255


def _truncate_bytes(text: str, budget: int) -> str:
    """`text` cut to at most `budget` bytes, **on a character boundary**.

    A filename is text; cutting a UTF-8 sequence in half produces a name no reader can
    open, and the byte cap is the one place where bytes and characters can disagree.
    """
    if budget <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    cut = encoded[:budget]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def fit_filename(stem: str, suffix: str) -> str:
    """`stem + suffix` shortened so the whole name fits, keeping `suffix` **whole**.

    One function for the three sites that build a memory's name (`MemoryFile.filename`,
    `MemoryStore._resolve_filename`, `promote_to_project`), because the alternative —
    each site truncating its own way — is how the collision suffix gets cut off, and the
    suffix is load-bearing: `_resolve_filename` finds a free name by counting upwards,
    so a name that no longer changes with the counter is the same name forever, i.e. a
    loop that never ends. Keeping the suffix whole is what makes that loop terminate.
    """
    budget = FILENAME_MAX_BYTES - len(suffix.encode("utf-8"))
    return _truncate_bytes(stem, budget) + suffix


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


# A run of whitespace, a line break included — every character that cannot
# appear in a line of a file.
_ROW_LABEL_WHITESPACE = re.compile(r"\s+")


def _index_row_label(title: str) -> str:
    """The label an index row can carry: one line, and one delimiter.

    A row is a **line** of the index, and both readers of one — ``MemoryIndex.from_text``
    and ``scripts/check-memory-index.py`` — take the target from the first ``](``
    after the opening ``[``. A title is neither of those: it is whatever the
    memory's first heading says, so it may hold a line break or the delimiter
    itself, and either makes the row unreadable to the readers the index exists
    for. Measured 2026-10-01 on a store this module's own API built:

    * a title holding a newline wrote the row as **two lines**. ``MemoryIndex``
      parsed the index as **zero entries** — the memory the index exists to point
      at was gone from it — while ``is_index_row`` still counted the first line
      as a row, so ``scripts/check-memory-index.py`` read ``row links 0 - no row
      carries a ](target) link`` and **passed, rc=0**. The file also gained a
      line, and the line count is the number the hygiene rule is stated in; a
      later ``update()`` of that same memory then appended a second row naming
      ``project-line1.md``, a file that does not exist.
    * a title holding ``](`` parsed its row with the filename
      ``' in it](project-has-in-it.md'`` — not a file — so the row named nothing
      and the guard exited 1. And because ``add_entry`` de-duplicates by the
      **real** filename while the stored row parsed to a different one, every
      later write appended another identical row: measured one memory, two rows.

    Both are the writer's to prevent — the label is ours to render, and being one
    line with one delimiter is what makes a row renderable and readable at all.
    """
    label = _ROW_LABEL_WHITESPACE.sub(" ", title).strip()
    # Break the *sequence*, not the bracket: escaping `]` alone leaves `](` in
    # the label (backslash, bracket, paren — the same two characters both
    # readers search for). `\(` is a Markdown escape, and renders as `(`.
    return label.replace("](", "]\\(")


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
# it against the file's **byte** size (`MemoryStore._warn_index_thresholds` below,
# and the reflection prompt's hygiene note, which prints the reading as "N bytes"),
# while `EmrgServer._cap_memory_index` applies it as a **character** budget, because
# what it bounds is a prompt and prompts are counted in characters (the incident the
# cap came from is quoted that way: 77% of a 452,972-char prompt). The two disagree
# about one CJK index — measured 2026-09-24, a 30,024-char / 61,160-byte index fires
# the advisory while the cap truncates nothing, and the band where that happens is
# wide, since CJK runs ~3 bytes per character.
#
# The direction that matters holds for **both** of the cap's subjects, and it holds
# because the advisory sits on the base store, not on one of the two that inherit
# from it: `MemoryStore._save_index` calls `_warn_index_thresholds`, so a project
# index and a session index are each flagged before the cap can see them. That is
# structural rather than lucky — the cap can only truncate a file the advisory
# already flagged, because UTF-8 never encodes a string in fewer bytes than
# characters, so `chars > T` implies `bytes > T`. Until 2026-09-24 the advisory was a
# `SessionMemoryStore` method and the project half went unwatched, measured on one
# fixture: a 56,214-char project index was truncated with no advisory having looked
# at its size, while the same file as a session index fired the advisory first
# (issue #1581, whose remedy is this wiring).
#
# What the ordering still does not reach, named so it is not overread: the advisory
# fires on a **store write**. An index the agent edits with `write`/`edit` — which is
# how the memory instructions tell it to maintain `MEMORY.md` — bypasses this method
# at either scope, and the cap's own notice naming the file it cut (#1578) is all
# that file has. Mechanised for both scopes in
# `tests/test_memory_index_thresholds.py`.
#
# The budget is also the constant the *row rule* rests on, and that is what fixes its
# value. `daemon.MEMORY_INDEX_ROW_CAP` (100 lines, the host's compaction trigger) is
# safe only while a maximum-width index at that line count still fits here — and
# `INDEX_TITLE_MAX_CHARS` is a bound on a **line**, so the row's own newline is part of
# the file: `n` full rows occupy `n * (512 + 1)` chars. The derivation used to be
# written as `100 x 512 = 51,200`, i.e. exactly this budget, and it was false by the
# newlines — measured 2026-09-26, 100 maximum-width rows are 51,300 chars, so the cap
# truncated the index (keeping 50,787 = the first 99 rows, dropping the newest) while
# the compaction note's trigger, `lines > 100`, stayed silent. That is the one direction
# the row rule promises cannot happen, and it costs precisely the newest row — the
# failure class issue #1554 measures, since the cap keeps the head of an index whose
# rows append newest-last. The row cap is the host's number and stays; this budget is
# nobody's instruction, so it moves to where the arithmetic puts it: 100 x 513 = 51,300
# needs at least 51 KiB, and the thousand round up (52,224) leaves 924 chars of slack.
# That slack is the whole difference between the numbers — measured from the other side,
# 924 = 1.8 full rows, so the first line count at which the cap could truncate a
# maximum-width index is **102**, two lines past the trigger — which is the property to
# keep if this is ever retuned: no truncation without the note having fired. Read from
# both sides in `tests/test_memory_index_row_cap_fits_the_budget.py`.
INDEX_SIZE_WARN = 51 * 1024

# What makes a line a **row** of an index, by shape rather than by the store's entry
# grammar. Two shapes, because the indexes this project carries are written in both: a
# `- ` list line (the store's own render, `MemoryIndex._render_entry`, and the pointer
# lines an agent's compaction writes), and a **Markdown-table body row** — a line opening
# with `|` that is not the table's delimiter row (`| --- | --- |`, punctuation rather than
# an entry). The header counts: it is a line the embed pays for, and "which lines are
# entries" cannot be decided by position without a second rule to keep true.
#
# One home, two readers. The daemon's compaction trigger
# (`daemon._memory_index_compaction_note`) and `scripts/check-memory-index.py` both call
# `is_index_row` below, so the instruction an agent receives and the reading a host prints
# count the same lines. Spelled separately once — both as `- ` — a table index fell between
# them: measured 2026-10-01 on this host, `.emrg/memory/MEMORY.md` (a table) read `rows 0,
# longest 0 chars, over 512: 0` and `OK` while four of its rows were past the bound, and
# the trigger, seeing no rows either, drew no compaction note at all.
INDEX_ROW_LIST_PREFIX = "- "
_INDEX_ROW_TABLE = re.compile(r"^\|")
_INDEX_ROW_TABLE_DELIMITER = re.compile(r"^\|[\s:|-]*\|?\s*$")
# The indent both shapes may carry and still be what they are: **up to 3 spaces** of leading
# whitespace (CommonMark's block-level limit, the same one `check-vote-count.py`'s `_FENCE_RE`
# reads for a fence). Four spaces is an indented code block instead — a line the embed pays
# for, but not a row, and not a line this predicate's two readers should bound per row.
#
# Without this, both shapes fell out of the predicate one indentation level away from the
# incident above: measured 2026-10-01 on `890bf02`, a table whose rows were the *only* thing
# indented by 2 spaces read `rows 1, longest 31 chars, over 512: 0` and `OK` while a 604-char
# row sat in the same file, and an indented list row's link was not resolved at all.
_INDEX_ROW_INDENT = re.compile(r"^ {0,3}")


def is_index_row(line: str) -> bool:
    """Whether `line` is a row of a memory index, judged by shape.

    The one predicate the daemon's compaction trigger and
    `scripts/check-memory-index.py` both apply, so the trigger and the reading count the
    same lines (the two shapes, the indent they may carry, and the incident that made the
    second shape matter are at the constants above).
    """
    line = _INDEX_ROW_INDENT.sub("", line, count=1)
    if line.startswith(INDEX_ROW_LIST_PREFIX):
        return True
    return bool(_INDEX_ROW_TABLE.match(line)) and not _INDEX_ROW_TABLE_DELIMITER.match(line)


# A **fenced code block**: three or more backticks or tildes, indented at most three
# spaces (CommonMark). The run itself is captured, because a closer is *the same
# character, at least as long* — `~~~` inside a ``` block is literal text, and a shorter
# run does not close a longer one.
#
# Why the row reading needs this at all: a fence's content is literal text, which is
# exactly what a row that *documents* the index's own format is made of. Measured
# 2026-10-01 on a fixture: an index whose only `- ` line and only `](target)` link were
# inside a fenced example read `rows 1`, `row links 1, unresolved: 1` and exited **1** —
# a reading that fires on the documentation of its own subject, which is how a check
# teaches its reader to ignore it (issue #1793 measured the same defect for an *inline*
# code span). The store's own render (`MemoryIndex._render_entry`) writes no fence, so no
# row the store produces is hidden by this.
#
# **Only a closed fence hides anything.** An opening fence the file never closes is read
# as ordinary text, which is the conservative direction: CommonMark would run it to the
# end of the document, and a reading that did the same would print `rows 3 - within`
# about a file whose rows it had stopped looking at — the pass-shaped answer this
# family exists to prevent. A typo therefore costs a possible false alarm (today's
# behaviour) and never a silent gap.
_INDEX_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INDEX_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")


class IndexLines(NamedTuple):
    """An index's lines, split by whether a row can be written on them.

    A result type rather than a second function, because the two halves come out of *one*
    pass and a reader that asked separately could be wrong about the same file twice.

    :param unfenced: `(line number, line)` for every line outside a fence, in file order.
    :param fenced: the line numbers inside a **closed** fence - the lines read as literal
        text, and so never candidates for a row. The fence markers themselves are in
        neither half: a marker is not a row (no shape reaches it) and not the literal text
        either, which is why the count of hidden lines is this tuple's length and **not**
        `lines - len(unfenced)` - that arithmetic also counts the markers, and a report
        built on it says a file hides two lines for every one it does.
    """

    unfenced: tuple[tuple[int, str], ...]
    fenced: tuple[int, ...]


def index_lines(text: str) -> IndexLines:
    """An index's lines, split by what a reader may read a row out of.

    The companion of `is_index_row`, and there for the same reason: a fence's content is
    literal text, so a `- ` line or a `](target)` link inside one is a row or a link the
    *documentation* carries, not the index. The daemon's compaction trigger
    (`daemon._memory_index_compaction_note`) and `scripts/check-memory-index.py` both read
    their rows through this, so the note and the report count the same lines — the
    property `is_index_row` alone could not give them, because "is this line literal
    text" is a question about the lines *around* it.

    :param text: the index's whole text.
    :returns: the split described at `IndexLines`.
    """
    unfenced: list[tuple[int, str]] = []
    fenced: list[int] = []
    open_fence: Optional[str] = None
    body: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if open_fence is None:
            opener = _INDEX_FENCE_OPEN.match(line)
            if opener is None:
                unfenced.append((number, line))
                continue
            open_fence = opener.group(1)
            body = []
            continue
        closer = _INDEX_FENCE_CLOSE.match(line)
        if closer is not None:
            run = closer.group(1)
            if run[0] == open_fence[0] and len(run) >= len(open_fence):
                open_fence = None
                fenced.extend(number for number, _ in body)
                continue
        body.append((number, line))
    if open_fence is not None:
        # Never closed: read as ordinary text, and the docstring above says why.
        unfenced.extend(body)
    return IndexLines(tuple(unfenced), tuple(fenced))


#: A row's detail-file link: `](target)`. The same shape the store renders
#: (`MemoryIndex._render_entry`) and the one the memory instructions tell an agent to
#: write, so a row carrying several links is read as several.
ROW_LINK = re.compile(r"\]\(([^)]+)\)")

#: An inline code span, removed from a line before its links are read. A row that
#: *documents* the link shape writes it inside backticks, and in Markdown an inline code
#: span is literal text, not a link — a regular expression cannot tell a link from a
#: quotation of one. Measured 2026-10-01 on this host: the evolution index's row at line
#: 8 quotes the shape while describing it, and the reading reported that quotation as a
#: row link resolving to no file beside the index, i.e. a guard firing on its own
#: subject's documentation. Deliberately single-backtick (the form the indexes here use);
#: a nested or multi-backtick span is not a shape this repository's memory files carry,
#: and one rule stated is one rule to keep true.
CODE_SPAN = re.compile(r"`[^`]*`")


def _mask_code_spans(line: str) -> str:
    """`line` with every code span blanked — **at the same length**.

    Blanking rather than deleting, because a caller that has to report *where* the link
    it read sits (`MemoryIndex.from_text` slices a row's label out of the original line)
    cannot map offsets through a substitution that changes the string's length.
    """
    return CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)


def row_links(line: str) -> list[str]:
    """The links a row carries: every `](target)` outside a code span, in order.

    The third step of reading a row, beside `is_index_row` (whether a line *is* a row) and
    `index_lines` (whether it is literal text): what a row that passed both *says*. One
    home, two readers, by the same argument as its two companions — `MemoryIndex.from_text`
    takes a row's target from here, and so does the link reading in
    `scripts/check-memory-index.py`, which used to spell the rule itself.

    **That second reader is why this exists.** Spelled twice, the two drifted, which is
    what a rule with two homes does. Measured 2026-10-01, one line, two answers:

    ```
    - [how `](t.md)` is read](real.md) — rec: 26-10-01
    ```

    the guard read the link outside the code span — `real.md`, the file the row means —
    while the parser's own regex, having no code-span rule, took the first `](` it saw and
    filed the memory under `` `](t.md)` is read](real.md ``: a name no file has, so the
    memory left the index and every later write appended a second row for it. The same
    pair disagreed about fences — `index_lines` was shared, the parser built its entries
    from every line, so a fenced *example* of the row shape became an entry naming
    `gone.md`. Two readers answering differently about one line is the defect; one home
    is the fix.
    """
    return ROW_LINK.findall(_mask_code_spans(line))


#: The shape of a full row — `- [label](target) [status] — rec: …, evt: …` — matched
#: against a line with its code spans blanked, so the label may quote the delimiter
#: without being read as one (`row_links` carries the measurement). The label is
#: non-greedy and the target stops at the first `)`, which is what makes this and
#: `ROW_LINK` the same reading: both answer with the first link that is not a code span.
_INDEX_ROW_RE = re.compile(
    r"^-\s+\[(.+?)\]\((.+?)\)(?:\s+\[(\w+)\])?\s*[-—]\s*(.+)"
)


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

        Bounded by `FILENAME_MAX_BYTES`: a title longer than the filesystem's name limit
        used to derive a name no filesystem accepts, so `create()` failed with a bare
        `OSError` from a path operation (`_truncate_bytes` carries the measurement). The
        bound belongs here rather than at the write, because this is what the index row,
        the server's memory frame and the collision counter all read — a name that only
        the write path shortened would be a second name for the same memory.
        """
        prefix = f"{self.type}-" if self.type != "reference" else ""
        slug = slugify(self.title)
        return fit_filename(f"{prefix}{slug}", ".md")

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

    One input that promise does not cover, which ``to_markdown`` states at the
    line and which is worth knowing before anyone reaches for a save: a row
    longer than ``INDEX_TITLE_MAX_CHARS`` is **re-rendered from the parsed
    fields** rather than truncated, so whatever the row carried beyond its
    title, link, status and dates is dropped — per-row prose, and any identifier
    that lived only in the row. Measured 2026-09-24 on this host's evolution
    index (172 rows, 31 over the cap): a load → save took it from 108,491 to
    68,421 chars and brought it within the cap (0 rows over), while dropping
    text from 31 rows — and for 27 of them an identifier the row names is in no
    detail file. So a save is the wrong way to trim a drifted index (#1551) even
    though it satisfies the cap: the text has to be checked against the detail
    file first, row by row.
    """

    def __init__(self, entries: list[_IndexEntry] | None = None):
        self.entries: list[_IndexEntry] = entries or []
        # Document skeleton: the index file's own lines in order, and the
        # filename each entry line parsed to. A fresh index (nothing loaded)
        # starts from just the title, so every entry counts as new.
        self._lines: list[str] = ["# Memory Index", ""]
        self._src: dict[int, str] = {}

    # ── Mutation ───────────────────────────────────────────────

    def add_entry(self, mem: MemoryFile, *, filename: str | None = None) -> None:
        """Add or update a memory entry.

        Identifies entries by filename (since id is not in the index).

        :param filename: the name to file this entry under, when it differs from the one
            `mem` derives from its own title (`MemoryFile.filename` → `<type>-<slug>.md`).
            That derived name is what the store's `create` *writes*, so the two agree for
            a memory the store wrote — and disagree for every file named any other way,
            which is most of them: a memory file can be written by hand, by an agent's
            `write` tool, or under an older naming rule. An index row's link is a claim
            about a **file**, so the caller that knows the on-disk name passes it;
            `_rebuild_index` is that caller, the only one that walks the directory.
        """
        stored = filename or mem.filename
        # Remove existing entry with same filename
        self.entries = [e for e in self.entries if e.filename != stored]

        # Rant 2026-08-23T08:04:26 — write-time truncation (primary guard):
        # keep the stored index title bounded; the full title lives in the
        # standalone .md frontmatter, and the filename stays in the index line.
        title = _truncate_index_title(mem.display_title)

        self.entries.append(
            _IndexEntry(
                title=title,
                filename=stored,
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

    def carry_line(self, line: str, filename: str) -> None:
        """Keep one row **as written**: the line verbatim, filed under `filename`.

        For a caller that holds the row's text but not the file behind it — `_rebuild_index`
        carrying the row of a memory file it could not read. The line joins the document
        skeleton, which `to_markdown` emits as written, and the entry registered against it
        is what keeps the line from being dropped (`to_markdown` skips a line whose filename
        no entry claims) and what stops a second row being appended for the same file.

        Re-deriving the row from parsed fields instead is not equivalent, and the difference
        was measured: a row written `rec: 26-10-01` parses to a two-digit year, and
        `_render_entry`'s `_short_date` slice turns it into `rec: 26-10-01T0` — the row comes
        back with a date that is not a date. A row nobody could verify is kept, not rewritten.
        """
        self._lines.append(line)
        self._src[len(self._lines) - 1] = filename
        self.entries.append(_IndexEntry(filename=filename, raw=line))

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
        # The label, not the title: a title may hold a line break or the row's
        # own delimiter, and a row carrying either is a row neither reader can
        # read (`_index_row_label` has the measurements). A title that renders to
        # nothing falls back to the filename, because an empty `[]` is not a row
        # `MemoryIndex.from_text` parses either.
        label = _index_row_label(e.title) or e.filename
        line = f"- [{label}]({e.filename}){status_tag} — {date_part}"
        if len(line) <= INDEX_TITLE_MAX_CHARS:
            return line
        # Rant 2026-08-23T08:04:26 — render-time fallback for legacy dirty
        # index data (write-time truncation wasn't in place). Keep the filename
        # so the detail file stays reachable.
        logger.warning(
            "memory index line exceeds %d chars (title=%d chars) — truncating",
            INDEX_TITLE_MAX_CHARS, len(label),
        )
        other = len(f"- []({e.filename}){status_tag} — {date_part}")
        budget = max(1, INDEX_TITLE_MAX_CHARS - other)
        return (
            f"- [{_truncate_index_title(label, budget)}]"
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
        # Both questions about a row are asked of the shared functions — `index_lines`
        # for whether a line may carry one at all, `row_links` for what it says — because
        # this parser used to answer both itself and so disagreed with
        # `scripts/check-memory-index.py` about the same file (the measurements are at
        # `row_links`). A fenced example of the row shape is documentation, not an entry.
        fenced = set(index_lines(text).fenced)

        for i, line in enumerate(lines):
            if (i + 1) in fenced:
                continue
            stripped = line.strip()

            # Detect type heading: ## user
            tm = re.match(r"^##\s+(\w+)", stripped)
            if tm and tm.group(1) in VALID_TYPES:
                current_type = tm.group(1)
                continue

            # Detect entry: - [Title](file.md) [status] — rec: ..., evt: ...
            # Matched against the line with its code spans blanked, so a label quoting
            # the delimiter is not read as one; the target itself comes from the shared
            # rule rather than from the group beside the label.
            masked = _mask_code_spans(line)
            body = masked.strip()
            offset = masked.index(body) if body else 0
            em = _INDEX_ROW_RE.match(body)
            targets = row_links(line)
            if em and targets:
                # The label as *written*: blanking keeps the length, so the match's
                # offsets still index this line.
                title = line[offset + em.start(1) : offset + em.end(1)]
                filename = targets[0]
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
        self._warn_index_thresholds()

    # Rant 2026-08-23T08:04:26 — soft guard only: warn when an index crosses a
    # threshold. Actual consolidation (merge/trim to ≤50) is LLM-driven via
    # reflection/consolidation prompts — never auto-delete here.
    #
    # On the base class since 2026-09-24, because the embed cap truncates *both*
    # indexes a store writes and the advisory used to be wired to one of them: as a
    # `SessionMemoryStore` method it left a project index to cross the cap with no
    # reader ever having measured its size, falsifying the ordering the cap's own
    # comment relied on (issue #1581; measured on one fixture — 56,214 chars, the
    # project store silent, the same file as a session index flagged). Here it covers
    # whichever index the store owns, since it reads only `self.index_path` and
    # `self.count`.
    #
    # Still out of reach, named so this is not read as a guarantee: an index written
    # by the agent's `write`/`edit` tools — which is how the memory instructions tell
    # it to maintain `MEMORY.md` — never reaches this method, at either scope.
    def _warn_index_thresholds(self) -> None:
        """Log a warning when this store's index exceeds a soft threshold."""
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

    def _rebuild_index(self) -> MemoryIndex:
        """The index the files on disk make, and the files the walk could not read.

        Two things this walk must not do, both measured 2026-10-01 on this repo's own
        memory directory (27 entries, every file named by hand or by an earlier rule):

        * **Name a row after anything but the file.** The entry's filename used to be
          `MemoryFile.filename`, i.e. `<type>-<slug>.md` derived from the *title* — the
          name `create` writes, and not the name a file written by hand has. Measured on
          that directory: the rebuild re-rendered every row with a target that exists
          nowhere, **27 of 27 links dead after it ran**, so the repair path for a broken
          index turned a working index into a set of links that resolve to nothing. The
          name to file an entry under is the one the file has.
        * **Drop the row of a file it could not read.** The tolerant catch here is
          `(OSError, ValueError)` and `UnicodeDecodeError` is a `ValueError` subclass, so a
          file another writer is mid-rewrite is skipped — the shape `_scan` documents at
          length (measured there: 37 of 121 reads raised while a 200-KB index was being
          rewritten, because `MemoryIndex.save` truncates and then writes). Skipping such a
          file here is not a skip: the whole index is rewritten from this walk, so the row
          goes with it, and the file is still on disk with nothing left naming it. The row
          the current index holds for it is therefore **carried over verbatim** — the file
          is there, it is the *read* that failed — and the file is reported to the caller.

        What a rebuild may still drop is a row whose file is **gone**: that pruning is what
        a rebuild is for, and the one case where the old row is a claim about something
        that is no longer there.

        :returns: the rebuilt index, and `(path, exception name)` per file that could not
            be read — the second half is what lets a caller say the rebuild was partial
            rather than report a walk it did not complete.
        """
        previous = {entry.filename: entry for entry in self._load_index().entries}
        idx = MemoryIndex()
        unreadable: list[tuple[Path, str]] = []
        for path in sorted(self.directory.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                mem = MemoryFile.from_file(path)
            except (OSError, ValueError) as exc:
                unreadable.append((path, type(exc).__name__))
                logger.debug("memory file could not be read: %s", path, exc_info=True)
                carried = previous.get(path.name)
                if carried is not None and carried.raw:
                    idx.carry_line(carried.raw, path.name)
                continue
            idx.add_entry(mem, filename=path.name)
        return idx, unreadable

    # ── File lookup ────────────────────────────────────────────

    def _scan(self, mem_id: str) -> tuple[Path | None, list[tuple[Path, str]]]:
        """One walk: the file carrying `mem_id`, and the files that could not be read.

        Returns `(path, unreadable)`, where `unreadable` pairs each file that raised
        with the exception's *name*. Both halves come from the same walk on purpose:
        a reader needs them together, and two functions doing the same walk would be
        two answers to "which file is this id in".

        Why the second half is collected rather than dropped: the tolerant catch here
        is `(OSError, ValueError)` and `UnicodeDecodeError` is a `ValueError`
        subclass, so a file another writer is mid-rewrite is skipped **silently**.
        `MemoryIndex.save` writes with `path.write_text` (truncate, then write), so a
        reader can observe a partial file, and a cut inside a multi-byte character is
        a decode error. A caller that then reports "not found" is making a claim its
        walk did not measure. Measured 2026-09-24 on a 200-KB memory file under a
        concurrent writer, through the daemon's `read_memory` frame: **68 of 120**
        requests answered "Memory not found" for a memory that was on disk, and 41
        raised `UnicodeDecodeError` out of the message loop.

        Parse failures are collected too, and that is not an overreach: a file whose
        frontmatter cannot be parsed is one whose id cannot be read either, so
        "not found" is equally unproven for it.
        """
        unreadable: list[tuple[Path, str]] = []
        for path in sorted(self.directory.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                mem = MemoryFile.from_file(path)
                if mem.id == mem_id:
                    return path, unreadable
            except (OSError, ValueError) as exc:
                unreadable.append((path, type(exc).__name__))
                logger.debug("Skipping unparseable memory: %s", path, exc_info=True)
        return None, unreadable

    def _find_by_id(self, mem_id: str) -> Path | None:
        """Scan directory for a .md file whose frontmatter id matches."""
        return self._scan(mem_id)[0]

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
            # Fitted with the suffix kept whole: the counter must survive the byte cap,
            # because this loop ends by the name changing — a name truncated down to a
            # constant would be the same candidate forever (`fit_filename`).
            candidate = fit_filename(stem, f"-{counter}.md")
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
        return self.get_with_reason(mem_id)[0]

    def get_with_reason(self, mem_id: str) -> tuple[MemoryFile | None, str]:
        """The memory with this id, or `(None, why it is not here)`.

        `why` is `""` for an absence the walk actually measured — what every caller
        of `get()` already acts on — and a sentence naming what could not be read
        otherwise. The two are different answers and a client cannot tell them apart
        from `None` alone: one says "there is no such memory", the other says "I could
        not finish looking", and only the first is true here.

        The second read is the one that used to raise: `_scan` reads every file to
        match the id, and then the chosen file is read **again** to return it. A
        writer that lands between the two turns a match into a `UnicodeDecodeError`
        escaping `get()` — measured 41 of those in the same 120 requests that
        produced the 68 false absences above. Returning the reason keeps a torn read
        out of the caller's stack: `get()` has no answer for it, and the caller that
        does (a client frame) needs it as data, not as an exception.
        """
        path, unreadable = self._scan(mem_id)
        if path is not None:
            try:
                return MemoryFile.from_file(path), ""
            except (OSError, ValueError) as exc:
                return None, (
                    f"{path.name} could not be read ({type(exc).__name__})"
                )
        if unreadable:
            names = ", ".join(f"{p.name} ({how})" for p, how in unreadable)
            return None, (
                f"{len(unreadable)} file(s) in the memory directory could not be "
                f"read: {names}"
            )
        return None, ""

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

    def rebuild_index(self) -> int:
        """Rebuild MEMORY.md from all .md files on disk.

        Useful if the index gets out of sync or corrupted.

        The rebuild is a **rewrite**, so what the walk missed is what the file loses; a
        file it could not read keeps the row the replaced index had for it (the reasons
        are in `_rebuild_index`), and the files it could not read are named in a warning —
        a partial walk is reported rather than passed off as a complete rebuild.

        :returns: the number of rows the rebuilt index holds.
        """
        index, unreadable = self._rebuild_index()
        self._save_index(index, "rebuild_index")
        logger.info(
            "index rebuilt: %d entries in %s", len(index.entries), self.index_path
        )
        if unreadable:
            logger.warning(
                "index rebuilt from a partial read: %d memory file(s) could not be read "
                "and their rows were carried over — %s",
                len(unreadable),
                ", ".join(f"{path.name} ({why})" for path, why in unreadable),
            )
        return len(index.entries)

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
        # Handle filename conflicts in project store. Fitted the same way `_resolve_filename`
        # fits its candidates — same bound, same suffix-kept-whole rule — so the copy's name
        # cannot overflow the filesystem's limit here either.
        base_stem = Path(mem.filename).stem
        counter = 1
        while new_path.exists():
            new_path = project_store.directory / fit_filename(base_stem, f"-{counter}.md")
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

        # Create .promoted marker. Fitted like every other name built from a memory's
        # filename: `.promoted` is six bytes longer than `.md`, so on a name already at
        # the bound `with_suffix` overflowed it — measured 2026-10-01 with a 400-character
        # title, whose promotion wrote the copy and then failed here with
        # `OSError: [Errno 63] File name too long` on the marker.
        marker = self.directory / fit_filename(Path(path.name).stem, ".promoted")
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
