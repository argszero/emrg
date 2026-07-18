"""Chat message row widget — role-colored message display.

Supported roles: user, assistant, system, tool.
Each role gets a distinct color prefix (like Claude Code and Codex).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from rich.style import Style

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


ChatRole = Literal["user", "assistant", "system", "tool"]

_ROLE_PREFIX: dict[ChatRole, str] = {
    "user": "> ",
    "assistant": "● ",
    "system": "○ ",
    "tool": "◇ ",
}

_ROLE_STYLE: dict[ChatRole, str] = {
    "user": "bold cyan",
    "assistant": "bold magenta",
    "system": "dim",
    "tool": "bold green",
}


class ChatRow(Widget):
    """A single chat message row.

    Args:
        role: Message role (user/assistant/system/tool).
        content: Message text content.
        timestamp: Optional message timestamp.
    """

    def __init__(
        self,
        role: ChatRole = "assistant",
        content: str = "",
        timestamp: datetime | None = None,
    ) -> None:
        self.role = role
        self.content = content
        self.timestamp = timestamp
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def render(self, ctx: RenderContext) -> list[Line]:
        prefix = _ROLE_PREFIX.get(self.role, "  ")
        indent = " " * len(prefix)  # same width, no symbol
        role_style = Style.parse(_ROLE_STYLE.get(self.role, ""))
        lines: list[Line] = []

        # Only first line gets the role prefix; continuation lines indented
        content_lines = self.content.split("\n")
        for i, line_text in enumerate(content_lines):
            lead = prefix if i == 0 else indent
            # Prefix gets role-specific style (e.g., bold cyan for user),
            # content text gets default context style.
            spans = [
                Span(text=lead, style=role_style),
                Span(text=line_text, style=ctx.style),
            ]
            lines.append(Line(spans=spans, style=ctx.style))

        self._dirty = False
        return lines
