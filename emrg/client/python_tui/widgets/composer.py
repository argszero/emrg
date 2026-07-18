"""Composer widget — multi-line text input area.

The chat input field. Supports multi-line text, history navigation,
and slash-command entry. Consumer subscribes to 'submit' events.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


class Composer(Widget):
    """Multi-line text input area at bottom of viewport.

    Args:
        prompt: Prompt prefix (default: '> ').
        placeholder: Placeholder text when empty.
        history_size: Number of history entries to retain.
        on_submit: Callback when user submits text (Enter without shift).
    """

    _text: str
    _cursor: int  # Cursor position within text
    _history: deque[str]
    _history_index: int
    _dirty: bool

    def __init__(
        self,
        prompt: str = "> ",
        placeholder: str = "Type a message...",
        history_size: int = 100,
        on_submit: Callable[[str], None] | None = None,
    ) -> None:
        self.prompt = prompt
        self.placeholder = placeholder
        self._text = ""
        self._cursor = 0
        self._history = deque(maxlen=history_size)
        self._history_index = 0
        self._on_submit = on_submit
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    @property
    def text(self) -> str:
        return self._text

    def insert(self, char: str) -> None:
        """Insert character at cursor position."""
        self._text = self._text[:self._cursor] + char + self._text[self._cursor:]
        self._cursor += len(char)
        self._dirty = True

    def delete_backward(self) -> None:
        """Delete character before cursor."""
        if self._cursor > 0:
            self._text = self._text[:self._cursor - 1] + self._text[self._cursor:]
            self._cursor -= 1
            self._dirty = True

    def delete_forward(self) -> None:
        """Delete character at cursor."""
        if self._cursor < len(self._text):
            self._text = self._text[:self._cursor] + self._text[self._cursor + 1:]
            self._dirty = True

    def move_cursor_left(self) -> None:
        """Move cursor one position left."""
        if self._cursor > 0:
            self._cursor -= 1
            self._dirty = True

    def move_cursor_right(self) -> None:
        """Move cursor one position right."""
        if self._cursor < len(self._text):
            self._cursor += 1
            self._dirty = True

    def move_cursor_home(self) -> None:
        """Move cursor to start of line."""
        self._cursor = 0
        self._dirty = True

    def move_cursor_end(self) -> None:
        """Move cursor to end of line."""
        self._cursor = len(self._text)
        self._dirty = True

    def history_prev(self) -> None:
        """Navigate to previous history entry."""
        if self._history and self._history_index < len(self._history):
            if self._history_index == 0 and self._text:
                self._history.append(self._text)
                self._history_index = 0  # reset; length just grew
            self._history_index += 1
            idx = len(self._history) - self._history_index
            self._text = self._history[idx] if idx >= 0 else ""
            self._cursor = len(self._text)
            self._dirty = True

    def history_next(self) -> None:
        """Navigate to next history entry."""
        if self._history_index > 0:
            self._history_index -= 1
            if self._history_index == 0:
                # Restore the saved-in-progress text (now at end of history)
                idx = len(self._history) - 1
                self._text = self._history[idx] if self._history else ""
                if self._text == "":
                    self._text = ""
            else:
                idx = len(self._history) - self._history_index
                self._text = self._history[idx] if idx >= 0 else ""
            self._cursor = len(self._text)
            self._dirty = True

    def submit(self) -> str | None:
        """Submit current text. Returns None if empty."""
        text = self._text.strip()
        if not text:
            return None
        self._history.append(text)
        self._history_index = 0
        self._text = ""
        self._cursor = 0
        self._dirty = True
        if self._on_submit:
            self._on_submit(text)
        return text

    def render(self, ctx: RenderContext) -> list[Line]:
        """Render the composer with prompt, text, and cursor indicator."""
        display = self._text or self.placeholder
        is_placeholder = not self._text

        # Show cursor position
        if self._text and self._cursor < len(self._text):
            cursor_char = self._text[self._cursor]
            prefix = self._text[:self._cursor]
            suffix = self._text[self._cursor + 1:]
        else:
            cursor_char = " "
            prefix = self._text
            suffix = ""

        cursor_style = "reverse" if self._text else "dim"
        style = "dim" if is_placeholder else ""

        spans = [
            Span(text=self.prompt, style="bold cyan"),
            Span(text=prefix, style=style),
            Span(text=cursor_char, style=cursor_style),
            Span(text=suffix, style=style),
        ]

        self._dirty = False
        return [Line(spans=spans, style=ctx.style)]
