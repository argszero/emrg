"""Cell buffer, string interning pools, and diff engine.

Core rendering data structures following Claude Code's screen.ts design:
- CharPool / StylePool / HyperlinkPool: O(1) string interning
- Cell: packed integers (char_id, style_id, hyperlink_id, width)
- Buffer: 2D cell grid with double-buffering and diff
"""

from __future__ import annotations

import unicodedata
from array import array
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.style import Style as RichStyle


def _normalize_style(s):
    """Normalize a style value to a Rich Style object.

    Handles both Rich Style objects and string declarations like 'bold cyan'.
    """
    if s is None:
        return None
    if isinstance(s, str):
        from rich.style import Style

        return Style.parse(s)
    return s


class CellWidth(IntEnum):
    """Character cell width. From Claude Code's CellWidth const enum."""

    NARROW = 0
    WIDE = 1
    SPACER_TAIL = 2
    SPACER_HEAD = 3


@dataclass(slots=True)
class Cell:
    """A decoded cell — the view type for buffer access.

    Stored as packed integers in the buffer (not as Python objects).
    This struct exists only for the decoded representation.
    """

    char: str = " "
    style_id: int = 0
    hyperlink_id: int = 0
    width: CellWidth = CellWidth.NARROW

    @property
    def is_empty(self) -> bool:
        return self.char == " " and self.style_id == 0 and self.hyperlink_id == 0


class CharPool:
    """String interning pool for characters.

    Space = id 0, empty = id 1.
    ASCII fast-path: array lookup for codes 0-127.
    """

    def __init__(self) -> None:
        self._strings: list[str] = [" ", ""]
        self._map: dict[str, int] = {" ": 0, "": 1}
        self._ascii: array = array("i", [-1]) * 128
        self._ascii[32] = 0  # space

    def intern(self, char: str) -> int:
        if len(char) == 1:
            code = ord(char)
            if code < 128:
                cached = self._ascii[code]
                if cached != -1:
                    return cached
                idx = len(self._strings)
                self._strings.append(char)
                self._ascii[code] = idx
                return idx
        existing = self._map.get(char)
        if existing is not None:
            return existing
        idx = len(self._strings)
        self._strings.append(char)
        self._map[char] = idx
        return idx

    def get(self, idx: int) -> str:
        return self._strings[idx] if idx < len(self._strings) else " "

    def __len__(self) -> int:
        return len(self._strings)


class StylePool:
    """String interning pool for Rich Style objects.

    Keys by a deterministic tuple of style properties (not hash()) to avoid
    Python hash collisions. Also caches ANSI transition strings.
    """

    def __init__(self) -> None:
        self._ids: dict[tuple, int] = {}  # style_key -> pool_id
        self._styles: list[RichStyle] = []
        self._transitions: dict[tuple[int, int], str] = {}
        self.none: int = self._intern_empty()

    @staticmethod
    def _style_key(style: RichStyle) -> tuple:
        """Deterministic, collision-free key for a Rich Style."""
        return (
            style.color,
            style.bgcolor,
            style.bold,
            style.dim,
            style.italic,
            style.underline,
            style.strike,
            style.reverse,
            style.conceal,
            style.blink,
            style.blink2,
            style.overline,
            style.link,
        )

    def _intern_empty(self) -> int:
        """Intern the default empty style."""
        from rich.style import Style

        key = self._style_key(Style())
        self._ids[key] = 0
        self._styles.append(Style())
        return 0

    def intern(self, style: RichStyle) -> int:
        key = self._style_key(style)
        existing = self._ids.get(key)
        if existing is not None:
            return existing
        idx = len(self._styles)
        self._styles.append(style)
        self._ids[key] = idx
        return idx

    def get(self, idx: int) -> RichStyle:
        """Return the style at the given pool index.

        Returns the default empty style (id 0) for idx == -1, which is the
        sentinel used by write_frame to force a style transition on the first
        diff cell. Python interprets -1 as the last list element by default,
        so we guard against that here.
        """
        if idx == -1:
            return self._styles[0]
        return self._styles[idx] if idx < len(self._styles) else self._styles[0]

    def transition(self, from_id: int, to_id: int) -> str:
        """Return cached ANSI string to transition between two styles."""
        if from_id == to_id:
            return ""
        key = (from_id, to_id)
        cached = self._transitions.get(key)
        if cached is not None:
            return cached
        from emrg.client.python_tui.output import style_diff_sgr

        result = style_diff_sgr(self.get(from_id), self.get(to_id))
        self._transitions[key] = result
        return result

    def __len__(self) -> int:
        return len(self._styles)


class HyperlinkPool:
    """String interning pool for OSC-8 hyperlinks. Id 0 = no hyperlink."""

    def __init__(self) -> None:
        self._strings: list[str] = [""]
        self._map: dict[str, int] = {}

    def intern(self, uri: str | None) -> int:
        if not uri:
            return 0
        existing = self._map.get(uri)
        if existing is not None:
            return existing
        idx = len(self._strings)
        self._strings.append(uri)
        self._map[uri] = idx
        return idx

    def get(self, idx: int) -> str | None:
        if idx == 0:
            return None
        return self._strings[idx] if idx < len(self._strings) else None

    def __len__(self) -> int:
        return len(self._strings)


@dataclass
class Buffer:
    """Double-buffered 2D cell grid.

    Cells are stored as packed integers in flat arrays (like Claude Code's Int32Array).
    Two 32-bit words per cell: word0=charId, word1=styleId|hyperlinkId|width.
    """

    width: int
    height: int
    char_pool: CharPool = field(default_factory=CharPool)
    style_pool: StylePool = field(default_factory=StylePool)
    hyperlink_pool: HyperlinkPool = field(default_factory=HyperlinkPool)
    _cells0: array = field(default_factory=lambda: array("I"), init=False, repr=False)
    _cells1: array = field(default_factory=lambda: array("I"), init=False, repr=False)

    # Cell word1 bit layout (from Claude Code):
    STYLE_SHIFT = 17
    HYPERLINK_SHIFT = 2
    HYPERLINK_MASK = 0x7FFF
    WIDTH_MASK = 3

    def __post_init__(self) -> None:
        self._resize_arrays(self.width * self.height)

    def _resize_arrays(self, size: int) -> None:
        """Resize cell arrays to exactly `size` elements — grow or shrink."""
        delta = size - len(self._cells0)
        if delta > 0:
            self._cells0.extend([0] * delta)
            self._cells1.extend([0] * delta)
        elif delta < 0:
            del self._cells0[size:]
            del self._cells1[size:]

    def _index(self, x: int, y: int) -> int:
        return y * self.width + x

    def get_cell(self, x: int, y: int) -> Cell:
        idx = self._index(x, y)
        char_id = self._cells0[idx]
        word1 = self._cells1[idx]
        style_id = word1 >> self.STYLE_SHIFT
        hyperlink_id = (word1 >> self.HYPERLINK_SHIFT) & self.HYPERLINK_MASK
        width = CellWidth(word1 & self.WIDTH_MASK)
        return Cell(
            char=self.char_pool.get(char_id),
            style_id=style_id,
            hyperlink_id=hyperlink_id,
            width=width,
        )

    def set_cell(self, x: int, y: int, cell: Cell) -> None:
        idx = self._index(x, y)
        char_id = self.char_pool.intern(cell.char)
        hyperlink_id = self.hyperlink_pool.intern(
            self.hyperlink_pool.get(cell.hyperlink_id)
        ) if cell.hyperlink_id else 0
        word1 = (
            (cell.style_id << self.STYLE_SHIFT)
            | ((hyperlink_id & self.HYPERLINK_MASK) << self.HYPERLINK_SHIFT)
            | (int(cell.width) & self.WIDTH_MASK)
        )
        self._cells0[idx] = char_id
        self._cells1[idx] = word1

    def clear(self) -> None:
        """Reset all cells to empty."""
        size = self.width * self.height
        for i in range(size):
            self._cells0[i] = 0
            self._cells1[i] = 0

    def resize(self, width: int, height: int) -> None:
        """Resize buffer — grow or shrink."""
        self.width = width
        self.height = height
        self._resize_arrays(width * height)


def diff_buffers(prev: Buffer, curr: Buffer) -> list[tuple[int, int, Cell, Cell]]:
    """Compare two buffers, return list of changed cells.

    Returns (x, y, prev_cell, curr_cell) for each different cell.
    When prev is taller than curr, extra rows emit clear operations
    (curr=empty Cell) so the terminal cleans up ghost content.
    O(n) scan with O(1) integer comparison per cell.
    """
    results: list[tuple[int, int, Cell, Cell]] = []
    min_h = min(prev.height, curr.height)
    w = min(prev.width, curr.width)
    empty = Cell()  # Space, style=0, hyperlink=0 — terminal default

    for y in range(min_h):
        prev_base = y * prev.width
        curr_base = y * curr.width
        prev_row0 = prev._cells0[prev_base : prev_base + w]
        curr_row0 = curr._cells0[curr_base : curr_base + w]
        prev_row1 = prev._cells1[prev_base : prev_base + w]
        curr_row1 = curr._cells1[curr_base : curr_base + w]

        for x in range(w):
            if prev_row0[x] != curr_row0[x] or prev_row1[x] != curr_row1[x]:
                results.append((x, y, prev.get_cell(x, y), curr.get_cell(x, y)))

    # If prev is taller, clear rows beyond new viewport height
    if prev.height > curr.height:
        for y in range(curr.height, prev.height):
            prev_base = y * prev.width
            for x in range(min(w, prev.width)):
                prev_cell = prev.get_cell(x, y)
                if not prev_cell.is_empty:
                    results.append((x, y, prev_cell, empty))

    return results


def write_lines_to_buffer(
    buf: Buffer,
    lines: list[object],
    start_row: int = 0,
    style_pool: object | None = None,
) -> None:
    """Write rendered Line/Span objects into a cell buffer.

    Converts each Line (list of Span) to cell entries.
    Spans carry Rich Style objects; they are interned through the buffer's style pool.

    Args:
        buf: Target buffer to write into.
        lines: List of Line objects (from widget.render()).
        start_row: Row offset within the buffer.
        style_pool: Optional style pool override (defaults to buf.style_pool).
    """
    pool = style_pool if style_pool is not None else buf.style_pool

    for row_idx, line in enumerate(lines):
        y = start_row + row_idx
        if y >= buf.height:
            break

        x = 0
        line_style = getattr(line, "style", None)
        spans = getattr(line, "spans", [])

        for span in spans:
            text = getattr(span, "text", "")
            span_style = getattr(span, "style", None)

            # Cascade: span style patches over line style
            span_style_n = _normalize_style(span_style)
            line_style_n = _normalize_style(line_style)
            if span_style_n and line_style_n:
                final_style = line_style_n + span_style_n
            else:
                final_style = span_style_n or line_style_n

            for ch in text:
                if x >= buf.width:
                    break

                # Skip newlines (they indicate new Line, not rendered)
                if ch == "\n":
                    continue

                cat = unicodedata.category(ch)
                # Skip combining marks, ZWNJ, ZWJ, ZWNBS
                if cat.startswith("M") or ch in ("​", "‌", "‍", "﻿"):
                    continue

                style_id = pool.intern(final_style) if final_style else 0
                is_wide = unicodedata.east_asian_width(ch) in ("W", "F")
                w = CellWidth.WIDE if is_wide else CellWidth.NARROW

                if is_wide and x + 1 < buf.width:
                    buf.set_cell(x, y, Cell(char=ch, style_id=style_id, width=CellWidth.WIDE))
                    buf.set_cell(x + 1, y, Cell(char="", style_id=style_id, width=CellWidth.SPACER_TAIL))
                    x += 2
                else:
                    buf.set_cell(x, y, Cell(char=ch, style_id=style_id, width=CellWidth.NARROW))
                    x += 1
