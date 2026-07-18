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
        style: Default style to apply (cascades like Ratatui's Line.style).
    """

    width: int
    style: Style = field(default_factory=Style)

    def __repr__(self) -> str:
        return f"RenderContext(w={self.width})"


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
