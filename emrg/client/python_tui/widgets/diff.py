"""Diff widget — unified or split diff with syntax highlighting.

Renders git-style diffs with color-coded additions/deletions.
Color scheme follows Codex's conventions: green for additions, red for deletions.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import unified_diff
from typing import Literal

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


DiffMode = Literal["unified", "split"]


class Diff(Widget):
    """Render a diff between old and new text.

    Args:
        old: Original text.
        new: New text.
        old_label: Label for old version (default: 'old').
        new_label: Label for new version (default: 'new').
        mode: 'unified' (single column) or 'split' (side-by-side).
    """

    def __init__(
        self,
        old: str = "",
        new: str = "",
        old_label: str = "old",
        new_label: str = "new",
        mode: DiffMode = "unified",
    ) -> None:
        self.old = old
        self.new = new
        self.old_label = old_label
        self.new_label = new_label
        self.mode = mode
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def render(self, ctx: RenderContext) -> list[Line]:
        if self.mode == "unified":
            return self._render_unified(ctx)
        return self._render_split(ctx)

    def _render_unified(self, ctx: RenderContext) -> list[Line]:
        """Render unified diff (single column with +/- markers)."""
        lines: list[Line] = []

        # Header
        lines.append(Line(
            spans=[Span(text=f"--- {self.old_label}", style="bold")],
            style=ctx.style,
        ))
        lines.append(Line(
            spans=[Span(text=f"+++ {self.new_label}", style="bold")],
            style=ctx.style,
        ))

        # Diff hunks
        diff = list(unified_diff(
            self.old.splitlines(keepends=True),
            self.new.splitlines(keepends=True),
            fromfile=self.old_label,
            tofile=self.new_label,
        ))

        for diff_line in diff[2:]:  # Skip the --- and +++ header lines
            if diff_line.startswith("@@"):
                lines.append(Line(
                    spans=[Span(text=diff_line.rstrip(), style="bold cyan")],
                    style=ctx.style,
                ))
            elif diff_line.startswith("+"):
                lines.append(Line(
                    spans=[Span(text=diff_line.rstrip(), style="green")],
                    style=ctx.style,
                ))
            elif diff_line.startswith("-"):
                lines.append(Line(
                    spans=[Span(text=diff_line.rstrip(), style="red")],
                    style=ctx.style,
                ))
            else:
                lines.append(Line(
                    spans=[Span(text=diff_line.rstrip(), style=ctx.style)],
                    style=ctx.style,
                ))

        self._dirty = False
        return lines

    def _render_split(self, ctx: RenderContext) -> list[Line]:
        """Render split diff (side-by-side, old on left, new on right).

        Each side gets half the available width minus separator.
        Context lines shown dim, deletions red (left), additions green (right).
        """
        half = max(20, (ctx.width - 3) // 2)  # 3 chars for " | " separator
        pad = " " * half

        old_lines = self.old.splitlines()
        new_lines = self.new.splitlines()
        max_lines = max(len(old_lines), len(new_lines))

        # Compute alignment using difflib (simple line-by-line matching)
        import difflib
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        opcodes = list(matcher.get_opcodes())

        lines: list[Line] = []
        # Header
        left_header = pad_line(self.old_label, half)
        right_header = pad_line(self.new_label, half)
        lines.append(Line(
            spans=[
                Span(text=left_header, style="bold"),
                Span(text=" | ", style="dim"),
                Span(text=right_header, style="bold"),
            ],
            style=ctx.style,
        ))

        # Process opcodes to build aligned output
        old_idx = 0
        new_idx = 0
        for tag, i1, i2, j1, j2 in opcodes:
            if tag == "equal":
                for k in range(i1, i2):
                    left = pad_line(old_lines[k] if k < len(old_lines) else "", half)
                    right = pad_line(new_lines[j1 + k - i1] if (j1 + k - i1) < len(new_lines) else "", half)
                    lines.append(Line(
                        spans=[Span(text=left, style="dim"), Span(text=" | ", style="dim"), Span(text=right, style="dim")],
                        style=ctx.style,
                    ))
            elif tag == "delete":
                for k in range(i1, i2):
                    left = pad_line(old_lines[k], half)
                    lines.append(Line(
                        spans=[Span(text=left, style="red"), Span(text=" | ", style="dim"), Span(text=pad, style=ctx.style)],
                        style=ctx.style,
                    ))
            elif tag == "insert":
                for k in range(j1, j2):
                    right = pad_line(new_lines[k], half)
                    lines.append(Line(
                        spans=[Span(text=pad, style=ctx.style), Span(text=" | ", style="dim"), Span(text=right, style="green")],
                        style=ctx.style,
                    ))
            elif tag == "replace":
                # Show both old and new side by side
                pairs = max(i2 - i1, j2 - j1)
                for p in range(pairs):
                    old_text = old_lines[i1 + p] if (i1 + p) < i2 else ""
                    new_text = new_lines[j1 + p] if (j1 + p) < j2 else ""
                    left = pad_line(old_text, half)
                    right = pad_line(new_text, half)
                    lines.append(Line(
                        spans=[Span(text=left, style="red"), Span(text=" | ", style="dim"), Span(text=right, style="green")],
                        style=ctx.style,
                    ))

        self._dirty = False
        return lines


def pad_line(text: str, width: int) -> str:
    """Pad or truncate text to exactly `width` characters."""
    if len(text) > width:
        return text[:width]
    return text.ljust(width)
