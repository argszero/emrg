"""Widget primitives for AI coding assistant TUIs.

Exports all widget classes AND the base types (Span, Line, RenderContext, CellWidth)
so consumers can build custom widgets without importing from internal modules.
"""

from emrg.client.python_tui.buffer import CellWidth
from emrg.client.python_tui.widgets.base import Line, RenderContext, Span, Widget
from emrg.client.python_tui.widgets.chat_row import ChatRow
from emrg.client.python_tui.widgets.composer import Composer
from emrg.client.python_tui.widgets.diff import Diff
from emrg.client.python_tui.widgets.markdown import Markdown, StreamingMarkdown
from emrg.client.python_tui.widgets.prompt import InlinePrompt
from emrg.client.python_tui.widgets.spinner import Spinner
from emrg.client.python_tui.widgets.status_line import StatusLine
from emrg.client.python_tui.widgets.table import Table
from emrg.client.python_tui.widgets.tool_card import ToolCard

__all__ = [
    "CellWidth",
    "Line",
    "RenderContext",
    "Span",
    "Widget",
    "ChatRow",
    "Composer",
    "Diff",
    "InlinePrompt",
    "Markdown",
    "Spinner",
    "StatusLine",
    "StreamingMarkdown",
    "Table",
    "ToolCard",
]
