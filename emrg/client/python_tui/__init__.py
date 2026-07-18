"""python-tui — A TUI rendering library for AI coding assistants.

Usage:
    from python_tui import Terminal, ChatRow, StreamingMarkdown, Composer, StatusLine, Span, Line

    term = Terminal()
    term.mount(composer=Composer(on_submit=handle), status=StatusLine(model="..."))
    term.append_to_scrollback(ChatRow(role="user", content="Hello"))
"""

from emrg.client.python_tui.events import InputParser
from emrg.client.python_tui.terminal import Terminal
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
    "Terminal",
    "Line",
    "RenderContext",
    "Span",
    "Widget",
    "ChatRow",
    "Composer",
    "Diff",
    "InlinePrompt",
    "InputParser",
    "Markdown",
    "Spinner",
    "StatusLine",
    "StreamingMarkdown",
    "Table",
    "ToolCard",
]
