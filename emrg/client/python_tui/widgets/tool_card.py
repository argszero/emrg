"""Tool card widget — one collapsed status row per tool execution.

Visualizes tool execution lifecycle as a state machine:
    pending → running → done (green check)
                     → failed (red X, the error carried for the detail view)

Cards render inline in the chat scrollback, not as modals.
Multiple concurrent tool cards stack naturally.

A card is **collapsed by definition** (rant 2026-09-30T09:17:54): its row is the
tool's name plus the agent's intent, and everything else — the input, the output —
is read in the tool-detail selector (``emrg/client/widgets.py::ToolSelector``),
which re-renders on every keystroke. The card is where that selector gets its
material, so the card owns its own arguments: ``app.py`` used to park them in a
temporary dict and drop them at ``tool_end``, which left nothing to show as input.

In-place expansion was removed with the Tab handler that drove it. It could not be
made to work: the terminal's viewport is write-only, so a card that has scrolled
into native scrollback cannot be repainted — the old handler expanded the earliest
unexpanded card, which was almost always already off-screen.
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
    """Collapsed tool execution status card, and the carrier of its own material.

    Args:
        name: Tool name (e.g., 'bash', 'read', 'write').
        command: The intent, or the formatted arguments — the row's second half.
        status: Current execution status.
        output: Tool output text, read by the detail selector.
        tool_call_id: The daemon's id for this call: what pairs the card with the
            result that answers it, on the live stream and in a replay alike.
        arguments: The call's arguments, as the single source of truth for the
            input the detail selector shows and for the edit/write summaries.
        elapsed: Seconds the call took, once it has ended.
    """

    name: str = ""
    command: str = ""
    status: ToolStatus = "pending"
    output: str = ""
    tool_call_id: str = ""
    arguments: dict = field(default_factory=dict)
    elapsed: float = 0.0
    _dirty: bool = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    @property
    def icon(self) -> str:
        """The status glyph — one definition, read by the card row and by the selector."""
        return _STATUS_ICONS.get(self.status, " ")

    def update(self, status: ToolStatus, output: str = "",
               elapsed: float | None = None) -> None:
        """Update tool status, and optionally its output and duration."""
        self.status = status
        if output:
            self.output = output
        if elapsed is not None:
            self.elapsed = elapsed
        self._dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        style_str = _STATUS_STYLES.get(self.status, "")

        # The row is the whole card: name + intent, on both clients (rant
        # 2026-09-30T09:17:54). No expand affordance is drawn because there is
        # nothing left to expand into — the detail lives in the selector, and a
        # chevron that promised an interaction the TUI does not have would be the
        # same defect the removed Tab handler was.
        header = (f"{self.icon} {self.name}: {self.command}"
                  if self.command else f"{self.icon} {self.name}")
        lines = [Line(
            spans=[Span(text=header, style=style_str)],
            style=ctx.style,
        )]

        self._dirty = False
        return lines
