"""Status line widget — single-line footer bar.

Displays model name, agent state, and other status info.
Layout (rant 2026-08-11T20:02:43): left = session + model + elapsed +
message count + dir (all core info), center = server id + host only.
No right section.
"""

from __future__ import annotations

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


class StatusLine(Widget):
    """Single-line status footer with two sections.

    Args:
        left: Left-aligned content (session title + short id + model).
        center: Center-aligned content (server id + host).
        model: Optional model display name (center fallback).
        left_elapsed: Optional elapsed-time string (e.g. ``[1:23]``) appended
            to the left section while busy.
        left_extra: Optional extra left content (e.g. ``· 3 msgs · ~/proj``).
    """

    def __init__(
        self,
        left: str = "",
        center: str = "",
        model: str | None = None,
        left_elapsed: str = "",
        left_extra: str = "",
    ) -> None:
        self.left = left
        self.center = center
        self._model = model
        self._left_elapsed = left_elapsed
        self._left_extra = left_extra
        self._dirty = True

    @property
    def elapsed(self) -> str:
        return self._left_elapsed

    @elapsed.setter
    def elapsed(self, value: str) -> None:
        self._left_elapsed = value
        self._dirty = True

    @property
    def left_elapsed(self) -> str:
        return self._left_elapsed

    @left_elapsed.setter
    def left_elapsed(self, value: str) -> None:
        self._left_elapsed = value
        self._dirty = True

    @property
    def left_extra(self) -> str:
        return self._left_extra

    @left_extra.setter
    def left_extra(self, value: str) -> None:
        self._left_extra = value
        self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    @property
    def model(self) -> str | None:
        return self._model

    @model.setter
    def model(self, value: str | None) -> None:
        self._model = value
        self._dirty = True

    def update(
        self,
        left: str | None = None,
        center: str | None = None,
        model: str | None = None,
        left_elapsed: str | None = None,
        left_extra: str | None = None,
    ) -> None:
        """Update any fields and mark dirty."""
        if left is not None:
            self.left = left
        if center is not None:
            self.center = center
        if model is not None:
            self._model = model
        if left_elapsed is not None:
            self._left_elapsed = left_elapsed
        if left_extra is not None:
            self._left_extra = left_extra
        self._dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        """Render a single-line status bar: [left] [center]."""
        # Build left section: session/model/elapsed/msg-count/dir
        left_parts: list[str] = []
        if self.left:
            left_parts.append(self.left)
        if self._left_elapsed:
            left_parts.append(self._left_elapsed)
        if self._left_extra:
            left_parts.append(self._left_extra)
        left_text = (" " + " ".join(left_parts)) if left_parts else ""

        center_text = self.center or self._model or ""

        # Layout: left fixed → center fills remaining
        width = ctx.width
        available_center = max(0, width - len(left_text))

        if center_text and available_center > 0:
            center_text = center_text.center(available_center)

        spans: list[Span] = []
        if left_text:
            spans.append(Span(text=left_text, style="bold magenta"))
        if center_text:
            spans.append(Span(text=center_text, style="dim"))

        self._dirty = False
        return [Line(spans=spans, style=ctx.style)]
