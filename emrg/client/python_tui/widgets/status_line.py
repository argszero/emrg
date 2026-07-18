"""Status line widget — single-line footer bar.

Displays token usage, model name, agent state, and other status info.
Follows Codex's StatusLineWidget pattern: left/center/right sections.
"""

from __future__ import annotations

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


class StatusLine(Widget):
    """Single-line status footer with three sections.

    Args:
        left: Left-aligned content (e.g., agent name).
        center: Center-aligned content (e.g., model name).
        right: Right-aligned content (e.g., token count).
        model: Optional model display name.
        tokens: Optional token usage count.
    """

    def __init__(
        self,
        left: str = "",
        center: str = "",
        right: str = "",
        model: str | None = None,
        tokens: int | None = None,
    ) -> None:
        self.left = left
        self.center = center
        self.right = right
        self._model = model
        self._tokens = tokens
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

    @property
    def tokens(self) -> int | None:
        return self._tokens

    @tokens.setter
    def tokens(self, value: int | None) -> None:
        self._tokens = value
        self._dirty = True

    def update(
        self,
        left: str | None = None,
        center: str | None = None,
        right: str | None = None,
        model: str | None = None,
        tokens: int | None = None,
    ) -> None:
        """Update any fields and mark dirty."""
        if left is not None:
            self.left = left
        if center is not None:
            self.center = center
        if right is not None:
            self.right = right
        if model is not None:
            self._model = model
        if tokens is not None:
            self._tokens = tokens
        self._dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        """Render a single-line status bar: [left] [center] [right]."""
        # Build text sections
        right_text = self.right
        if self._tokens is not None:
            right_text = f"↑ {self._tokens:,} tk  {right_text}"

        left_text = f" {self.left}" if self.left else ""
        center_text = self.center or self._model or ""

        # Layout: left fixed → center fills remaining → right fixed, right-aligned
        width = ctx.width
        fixed_width = len(left_text) + len(right_text)
        available_center = max(0, width - fixed_width)

        if center_text and available_center > 0:
            center_text = center_text.center(available_center)

        spans: list[Span] = []
        if left_text:
            spans.append(Span(text=left_text, style="bold magenta"))
        if center_text:
            spans.append(Span(text=center_text, style="dim"))
        if right_text:
            # Pad left side of right section to push it to the right edge
            right_pad = max(0, width - len(left_text) - len(center_text) - len(right_text))
            spans.append(Span(text=f"{' ' * right_pad}{right_text}", style="dim"))

        self._dirty = False
        return [Line(spans=spans, style=ctx.style)]
