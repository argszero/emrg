"""Table widget — tabular data display.

Renders structured data (model picker, file list, etc.) as Rich tables.
Follows Codex's pattern: cyan header, dim separators, default text for cells.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.table import Table as RichTable

from emrg.client.python_tui.widgets.base import Line, RenderContext, Widget


@dataclass
class Table(Widget):
    """Tabular data display via Rich Table.

    Args:
        headers: Column header labels.
        rows: List of row data (each row is a list of cell strings).
        title: Optional title displayed above the table.
    """

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    title: str = ""
    _dirty: bool = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    @dirty.setter
    def dirty(self, value: bool) -> None:
        self._dirty = value

    def render(self, ctx: RenderContext) -> list[Line]:
        from emrg.client.python_tui.rich_bridge import rich_renderable_to_lines

        table = RichTable(
            title=self.title if self.title else None,
            show_header=bool(self.headers),
            header_style="bold cyan",
            border_style="dim",
            expand=True,
        )
        for header in self.headers:
            table.add_column(header)
        for row in self.rows:
            table.add_row(*[str(cell) for cell in row])

        lines = rich_renderable_to_lines(table, ctx.width)
        for line in lines:
            line.style = ctx.style
        self._dirty = False
        return lines
