"""Markdown widget — static rendering via Rich.

Also provides StreamingMarkdown for incremental token-by-token rendering.
Wraps Rich's Markdown parser for syntax highlighting, code blocks, tables.

StreamingMarkdown holds back partial fenced code blocks until the fence closes,
preventing flickering syntax highlighting during streaming.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.markdown import Markdown as RichMarkdown

from emrg.client.python_tui.widgets.base import Line, RenderContext, Widget


class Markdown(Widget):
    """Static markdown renderer. Wraps Rich's Markdown.

    Args:
        text: Markdown source text.
    """

    def __init__(self, text: str = "") -> None:
        self.text = text
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def render(self, ctx: RenderContext) -> list[Line]:
        """Render markdown using Rich, convert to our Line/Span format."""
        from emrg.client.python_tui.rich_bridge import rich_renderable_to_lines

        md = RichMarkdown(self.text, code_theme="monokai")
        lines = rich_renderable_to_lines(md, ctx.width)
        for line in lines:
            line.style = ctx.style
        self._dirty = False
        return lines


@dataclass
class StreamingMarkdown(Widget):
    """Incremental markdown renderer for token-by-token streaming.

    Holds a buffer of received tokens. Each `.feed(tokens)` call appends
    and sets dirty. Partial fenced code blocks are held back (not rendered
    until the fence closes) to avoid flickering syntax highlighting.

    Args:
        code_theme: Pygments theme for code blocks (default: 'monokai').

    Usage:
        stream = StreamingMarkdown()
        for token in api_stream:
            stream.feed(token)
            term.mark_dirty("chat")
    """

    _buffer: str = ""
    _dirty: bool = True
    code_theme: str = "monokai"
    _last_rendered: list[Line] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def feed(self, tokens: str) -> None:
        """Append tokens to the streaming buffer. Triggers re-render."""
        self._buffer += tokens
        self._dirty = True

    def reset(self) -> None:
        """Clear the buffer for a new message."""
        self._buffer = ""
        self._dirty = True
        self._last_rendered.clear()

    def render(self, ctx: RenderContext) -> list[Line]:
        """Render current buffer state.

        Holds back partial fenced code blocks — if the buffer ends with an
        unclosed code fence, only renders content up to the fence start.
        """
        from emrg.client.python_tui.rich_bridge import rich_renderable_to_lines

        text = self._buffer

        # Check for unclosed fenced code block
        # Simple heuristic: count triple-backtick occurrences
        fence_count = text.count("```")
        if fence_count % 2 == 1:
            # Last fence is unclosed — render only up to it
            last_fence = text.rfind("```")
            text = text[:last_fence]

        md = RichMarkdown(text, code_theme=self.code_theme)
        lines = rich_renderable_to_lines(md, ctx.width)
        for line in lines:
            line.style = ctx.style

        self._last_rendered = lines
        self._dirty = False
        return lines

    @property
    def buffer(self) -> str:
        """Current buffer content (for consumer inspection)."""
        return self._buffer
