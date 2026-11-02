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


def format_message_time(timestamp) -> str:
    """The `HH:MM` a message row shows for the daemon's moment, or "" if unreadable.

    One formatter for every row kind, so a user message and an assistant reply
    cannot disagree about the format (rant 2026-10-09T09:25:00). The moment is
    the **daemon's** — read here, never produced: a client that stamped its own
    clock would make a message's time a property of whoever happened to be
    watching, and two clients would show the same record two ways.

    An absent or unreadable value renders as **nothing** rather than as an empty
    pair of brackets or `Invalid Date`: records written before the moment was
    carried have no time, and a defect visible only in their rendering is what
    returning "" avoids.

    The date is deliberately not in the row — a chat line carries the clock time,
    and the full `YYYY-MM-DD HH:MM` is where the whole record is shown
    (`RewindSelector` prints it for the message it is about to rewind to).
    """
    if not timestamp:
        return ""
    if isinstance(timestamp, datetime):
        at = timestamp
    else:
        try:
            at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return ""
    return at.strftime("%H:%M")


_CLOCK_GAP = "  "


def clock_width(clock: str) -> int:
    """Columns `clock` needs on a line, its gap included — the budget a row reserves.

    A clock is placed on a line **after** the content has been wrapped, so a row
    that wraps its content to the full width leaves the clock nowhere to go: it
    begins past the last column and the terminal clips it, which reads as "no
    clock at all" however faithfully the value was carried (issue #2668). Every
    renderer therefore subtracts this from the width it wraps to, and places the
    clock ahead of any padding the renderer left on the line.
    """
    return len(_CLOCK_GAP) + len(clock) if clock else 0


def place_clock(line: Line, clock: str) -> None:
    """Append `clock` to `line`, ahead of the padding already on it.

    The padding a markdown renderer pads a line with is part of its content span,
    so appending the clock after it puts the clock at the far edge and, once the
    clock's own width is in the wrap budget, still leaves the padding between the
    text and the moment. Trimming it first lets the clock follow the text, and it
    keeps the line within the width the row reserved (issue #2668).
    """
    if not clock or not line.spans:
        return
    last = line.spans[-1]
    stripped = last.text.rstrip()
    if stripped != last.text:
        last.text = stripped
        if not last.text and len(line.spans) > 1:
            line.spans.pop()
    line.spans.append(Span(text=f"{_CLOCK_GAP}{clock}", style="dim"))


class ChatRow(Widget):
    """A single chat message row.

    Args:
        role: Message role (user/assistant/system/tool).
        content: Message text content.
        timestamp: The daemon's moment for this message (ISO string, or a
            `datetime`); rendered as `HH:MM` on the first line (rant
            2026-10-09T09:25:00).
    """

    def __init__(
        self,
        role: ChatRole = "assistant",
        content: str = "",
        timestamp: datetime | str | None = None,
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

        # The moment rides the first line, dim, appended after the text — the
        # first line is the only one whose width the role prefix already consumes,
        # and a trailing clock reads as a margin note rather than as content.
        clock = format_message_time(self.timestamp)
        if clock and lines:
            place_clock(lines[0], clock)

        self._dirty = False
        return lines
