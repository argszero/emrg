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
        """Render the composer with prompt, text, and cursor indicator.

        Multi-line text (pasted input) renders as one Line per logical line:
        the first line carries the prompt, continuation lines a same-width
        indent, and the cursor is drawn on the line it currently sits in
        (rant 2026-08-19T14:25:55 — a single Line with embedded ``\\n`` was
        flattened by the buffer, which skips newline characters).
        """
        is_placeholder = not self._text

        if self._text:
            content_lines = self._text.split("\n")
            indent = " " * len(self.prompt)
            lines_out: list[Line] = []
            line_start = 0
            for i, line_text in enumerate(content_lines):
                line_end = line_start + len(line_text)
                # Cursor lives in this line iff it is within [line_start, line_end].
                cursor_here = line_start <= self._cursor <= line_end
                if cursor_here:
                    rel = self._cursor - line_start
                    if rel < len(line_text):
                        cursor_char = line_text[rel]
                        prefix = line_text[:rel]
                        suffix = line_text[rel + 1:]
                    else:  # cursor at end of this line (incl. on the newline)
                        cursor_char = " "
                        prefix = line_text
                        suffix = ""
                else:
                    cursor_char = " "
                    prefix = line_text
                    suffix = ""
                lines_out.append(Line(spans=[
                    Span(text=self.prompt if i == 0 else indent,
                         style="bold cyan" if i == 0 else "dim"),
                    Span(text=prefix, style="" if not is_placeholder else "dim"),
                    Span(text=cursor_char, style="reverse"),
                    Span(text=suffix, style="" if not is_placeholder else "dim"),
                ], style=ctx.style))
                line_start = line_end + 1  # skip the newline separator
            self._dirty = False
            return lines_out

        # Empty / placeholder: single line with prompt + dim cursor block.
        spans = [
            Span(text=self.prompt, style="bold cyan"),
            Span(text=" ", style="dim"),
            Span(text=" ", style="dim"),
        ]
        self._dirty = False
        return [Line(spans=spans, style=ctx.style)]
