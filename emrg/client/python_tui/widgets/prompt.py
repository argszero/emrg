"""Inline prompt widget — permission/approval prompts.

Rendered inline in the composer area, not as modal dialogs.
Follows Codex's approval_overlay.rs pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget


PromptChoice = Literal["ask", "always", "deny"]


@dataclass
class InlinePrompt(Widget):
    """Inline permission/approval prompt.

    Args:
        question: The permission question to display.
        options: Available choices with shortcut keys.
        default: Default choice (highlighted).

    Usage:
        prompt = InlinePrompt(
            question="Allow this tool to run?",
            options={"y": ("ask", "Yes"), "a": ("always", "Always"), "n": ("deny", "No")},
            default="ask",
        )
    """

    question: str = ""
    options: dict[str, tuple[PromptChoice, str]] = field(default_factory=dict)
    default: PromptChoice = "ask"
    selected: PromptChoice | None = None
    _dirty: bool = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def select(self, choice: PromptChoice) -> None:
        """Select a choice (called by key handler)."""
        self.selected = choice
        self._dirty = True

    def reset(self) -> None:
        """Reset for next prompt."""
        self.question = ""
        self.options.clear()
        self.selected = None
        self._dirty = True

    def render(self, ctx: RenderContext) -> list[Line]:
        if not self.question:
            return []

        lines: list[Line] = []

        # Question line
        lines.append(Line(
            spans=[Span(text=f"? {self.question}", style="bold cyan")],
            style=ctx.style,
        ))

        # Options line
        option_spans: list[Span] = []
        for key, (choice, label) in self.options.items():
            is_default = choice == self.default
            is_selected = choice == self.selected
            style = "bold reverse" if is_default else "dim"
            if is_selected:
                style = "bold green"
            option_spans.append(Span(text=f" [{key}] {label} ", style=style))

        if option_spans:
            lines.append(Line(spans=option_spans, style=ctx.style))

        self._dirty = False
        return lines
