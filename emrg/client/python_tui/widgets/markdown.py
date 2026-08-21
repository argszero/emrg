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


class UserMarkdown(Markdown):
    """User message rendered as markdown with the role prefix preserved.

    Plan B (rant 2026-08-18T18:52:45, superseding 18:50:14): user messages
    go through the same Rich markdown pipeline as assistant messages — free
    width-based wrapping, CJK wide-char handling — while keeping the
    ``> `` prefix + bold cyan role visual. The markdown is rendered at
    ``ctx.width - len(prefix)`` so the prefix on the first line never
    overflows the buffer width (continuation lines get a same-width indent).

    Single newlines are preserved as hard line breaks (rant
    2026-08-19T14:25:55): Rich would otherwise collapse ``\\n`` into a
    space, merging pasted multi-line messages into one wrapped line.
    """

    _ROLE_PREFIX = "> "
    _ROLE_STYLE = "bold cyan"

    def render(self, ctx: RenderContext) -> list[Line]:
        from rich.style import Style

        from emrg.client.python_tui.rich_bridge import rich_renderable_to_lines
        from emrg.client.python_tui.widgets.base import Span

        prefix = self._ROLE_PREFIX
        indent = " " * len(prefix)
        role_style = Style.parse(self._ROLE_STYLE)
        avail = max(1, ctx.width - len(prefix))

        md = RichMarkdown(_preserve_line_breaks(self.text), code_theme="monokai")
        md_lines = rich_renderable_to_lines(md, avail)
        lines: list[Line] = []
        for i, line in enumerate(md_lines):
            lead = prefix if i == 0 else indent
            line.spans.insert(0, Span(text=lead, style=role_style))
            line.style = ctx.style
            lines.append(line)
        self._dirty = False
        return lines


def _preserve_line_breaks(text: str) -> str:
    """Turn single newlines into hard breaks so RichMarkdown keeps them.

    Rich collapses a single ``\\n`` (markdown soft break) into a space, so
    pasted multi-line user messages render as one long auto-wrapped line.
    A CommonMark hard break is a line ending in two spaces — Rich renders
    each such line separately. Blank lines (paragraph separators) and the
    interior of fenced code blocks are left untouched: trailing whitespace
    is significant inside code blocks.
    """
    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
        elif in_fence or not line.strip():
            out.append(line)
        else:
            out.append(line + "  ")
    return "\n".join(out)


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
