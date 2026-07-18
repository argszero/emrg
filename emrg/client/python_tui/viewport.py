"""Viewport management — fixed region at bottom of terminal.

Divides the terminal into:
- Scrollback area (above viewport): terminal-native, selectable text
- Viewport (fixed region at bottom): active UI (composer, status, streaming output)

Region layout is explicit — no flexbox/grid. A chat UI has a simple, fixed layout:
  status:  1 row  (bottom)
  composer: N rows (above status)
  prompts:  1 row  (above composer, optional)
  chat:     remaining rows
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Viewport:
    """Fixed region at bottom of terminal for active UI.

    On SIGWINCH: resize adjusts viewport height. Scrollback is preserved.
    """

    viewport_height: int = 12  # Default: last 12 rows
    viewport_top: int = 0  # Computed: terminal_height - viewport_height
    viewport_width: int = 80
    terminal_height: int = 24
    terminal_width: int = 80

    # Region layout (row offsets from viewport bottom)
    status_height: int = 1
    composer_height: int = 3
    prompt_height: int = 0  # 0 when no prompt active

    def resize(self, terminal_height: int, terminal_width: int) -> None:
        """Handle terminal resize. Preserves scrollback above viewport."""
        self.terminal_height = terminal_height
        self.terminal_width = terminal_width
        self.viewport_width = terminal_width
        self.viewport_top = terminal_height - self.viewport_height

    @property
    def chat_height(self) -> int:
        """Remaining rows for chat after fixed regions."""
        return max(
            1,
            self.viewport_height
            - self.status_height
            - self.composer_height
            - self.prompt_height,
        )

    @property
    def status_region(self) -> tuple[int, int]:
        """(start_row, end_row) for status bar within viewport."""
        start = self.viewport_height - self.status_height
        return (start, self.viewport_height)

    @property
    def composer_region(self) -> tuple[int, int]:
        """(start_row, end_row) for composer within viewport."""
        start = self.viewport_height - self.status_height - self.composer_height
        end = self.viewport_height - self.status_height
        return (start, end)

    @property
    def prompt_region(self) -> tuple[int, int]:
        """(start_row, end_row) for inline prompt within viewport."""
        if self.prompt_height == 0:
            return (0, 0)
        start = (
            self.viewport_height
            - self.status_height
            - self.composer_height
            - self.prompt_height
        )
        end = self.viewport_height - self.status_height - self.composer_height
        return (start, end)

    @property
    def chat_region(self) -> tuple[int, int]:
        """(start_row, end_row) for chat area within viewport."""
        start = 0
        end = self.chat_height
        return (start, end)
