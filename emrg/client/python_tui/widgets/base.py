"""Widget protocol and rendering context.

Widgets are minimal — a `render(ctx) -> list[Line]` method and a `dirty` flag.
No retained tree, no lifecycle hooks, no CSS layout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from rich.style import Style

from emrg.client.python_tui.buffer import CellWidth  # noqa: F401 — re-export for widget consumers

__all__ = ["CellWidth", "Span", "Line", "RenderContext", "Widget"]


@dataclass(slots=True)
class Span:
    """A styled string segment. Equivalent to Ratatui's Span."""

    text: str
    style: Style = field(default_factory=Style)

    def __len__(self) -> int:
        return len(self.text)

    def __repr__(self) -> str:
        return f"Span({self.text!r})"


@dataclass(slots=True)
class Line:
    """A line of styled spans. Equivalent to Ratatui's Line."""

    spans: list[Span] = field(default_factory=list)
    style: Style = field(default_factory=Style)

    def __len__(self) -> int:
        return sum(len(s) for s in self.spans)

    def __repr__(self) -> str:
        return f"Line(spans={len(self.spans)})"


@dataclass(slots=True)
class RenderContext:
    """Context passed to widget render methods.

    Attributes:
        width: Available columns for this render region.
        height: Available **rows** for this render region, when the caller knows them.
            `0` means "not stated": a widget that cannot scroll then renders unbounded,
            because it must not invent a bound it was not given. The terminal fills this
            per region — each region's own row budget, which is what a widget that emits a
            window has to respect: the viewport draws only the **tail** of what it is
            handed and pushes the rest into native scrollback, where it is never redrawn
            (rants 2026-10-14T14:09:10 for the chat bound, 2026-10-14T15:28:56 for the
            composer's).
        style: Default style to apply (cascades like Ratatui's Line.style).
    """

    width: int
    height: int = 0
    style: Style = field(default_factory=Style)

    def __repr__(self) -> str:
        return f"RenderContext(w={self.width}, h={self.height})"


@runtime_checkable
class Widget(Protocol):
    """A renderable widget.

    The minimal widget protocol: objects with a `render` method and a `dirty` flag.
    No mount/unmount, no reactive state — the Terminal decides when to call render
    based on dirty tracking per viewport region.
    """

    def render(self, ctx: RenderContext) -> list[Line]:
        """Return lines to display. Pure function — no I/O, no side effects."""
        ...

    @property
    def dirty(self) -> bool:
        """Whether this widget needs re-render. Reset after render."""
        ...

    @dirty.setter
    def dirty(self, value: bool) -> None:  # noqa: F811
        ...
