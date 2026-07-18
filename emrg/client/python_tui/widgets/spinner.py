"""Spinner widget — animated spinner for running operations.

Simple frame-based animation. Each render() call advances one frame.
"""

from __future__ import annotations

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class Spinner(Widget):
    """Animated spinner with optional label text.

    Args:
        text: Label text shown after spinner.
    """

    def __init__(self, text: str = "") -> None:
        self.text = text
        self._frame = 0
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def tick(self) -> None:
        """Advance to next animation frame."""
        self._frame = (self._frame + 1) % len(_SPINNER_FRAMES)
        self._dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        frame_char = _SPINNER_FRAMES[self._frame]
        spans = [
            Span(text=frame_char, style="bold cyan"),
            Span(text=f" {self.text}", style=ctx.style),
        ]
        self._dirty = False
        return [Line(spans=spans, style=ctx.style)]
