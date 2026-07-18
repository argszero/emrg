"""Tool card widget — expandable tool execution status card.

Visualizes tool execution lifecycle as a state machine:
    pending → running → done (green check + output)
                     → failed (red X + error, expandable)

Cards render inline in the chat scrollback, not as modals.
Multiple concurrent tool cards stack naturally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


ToolStatus = Literal["pending", "running", "done", "failed"]


_STATUS_ICONS: dict[ToolStatus, str] = {
    "pending": "○",
    "running": "◐",
    "done": "✓",
    "failed": "✗",
}

_STATUS_STYLES: dict[ToolStatus, str] = {
    "pending": "dim",
    "running": "bold cyan",
    "done": "bold green",
    "failed": "bold red",
}


@dataclass
class ToolCard(Widget):
    """Expandable tool execution status card.

    Args:
        name: Tool name (e.g., 'bash', 'read', 'write').
        command: The command or operation being executed.
        status: Current execution status.
        output: Tool output text (shown when expanded).
        expanded: Whether the card is expanded to show output.
    """

    name: str = ""
    command: str = ""
    status: ToolStatus = "pending"
    output: str = ""
    expanded: bool = False
    _dirty: bool = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def update(self, status: ToolStatus, output: str = "") -> None:
        """Update tool status and optionally set dirty."""
        self.status = status
        if output:
            self.output = output
        self._dirty = True

    def toggle(self) -> None:
        """Toggle expand/collapse."""
        self.expanded = not self.expanded
        self._dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        icon = _STATUS_ICONS.get(self.status, " ")
        style_str = _STATUS_STYLES.get(self.status, "")

        lines: list[Line] = []

        # Header line: [icon] tool_name command
        expand_icon = "▼" if self.expanded else "▶"
        header = f"{expand_icon} {icon} {self.name}: {self.command}"
        lines.append(Line(
            spans=[Span(text=header, style=style_str)],
            style=ctx.style,
        ))

        # Expanded output
        if self.expanded and self.output:
            for output_line in self.output.split("\n"):
                lines.append(Line(
                    spans=[Span(text=f"  {output_line}", style="dim")],
                    style=ctx.style,
                ))

        self._dirty = False
        return lines
