"""Tests for UserMarkdown rendering (Plan B, rant 2026-08-18T18:52:45).

User messages render through the same Rich markdown pipeline as assistant
messages — width-based wrapping + CJK handling — while keeping the "> "
prefix + bold cyan role visual. Verifies:
- long user messages wrap, no content loss / buffer truncation
- markdown styling (**bold**, `code`) is preserved
- CJK wide chars wrap at display width (no misalignment)
- ChatHistory routes "user" → UserMarkdown; system/tool/assistant → ChatRow
- update_last() updates a UserMarkdown row
"""

from __future__ import annotations

from rich.cells import cell_len

from emrg.client.python_tui.buffer import Buffer, write_lines_to_buffer
from emrg.client.python_tui.widgets.base import RenderContext
from emrg.client.python_tui.widgets.chat_row import ChatRow
from emrg.client.python_tui.widgets.markdown import UserMarkdown
from emrg.client.widgets import ChatHistory


def _render(user_text: str, width: int) -> list[str]:
    ctx = RenderContext(width=width)
    lines = UserMarkdown(user_text).render(ctx)
    return ["".join(s.text for s in line.spans) for line in lines]


def test_user_markdown_wraps_long_message():
    """Long user message wraps; first line has '> ', continuations indented."""
    visible = _render(
        "This is a very long user message that should wrap into multiple lines", 20
    )
    assert len(visible) > 1
    assert visible[0].startswith("> ")
    assert all(line.startswith("  ") for line in visible[1:])
    for line in visible:
        assert cell_len(line) <= 20


def test_user_markdown_no_content_loss_in_buffer():
    """Rendered lines written to the cell buffer retain the full text."""
    text = "a reasonably long user message that should be fully visible after wrap"
    ctx = RenderContext(width=30)
    lines = UserMarkdown(text).render(ctx)
    buf = Buffer(width=30, height=10)
    write_lines_to_buffer(buf, lines)
    rows = []
    for y in range(10):
        cells = [buf.get_cell(x, y).char or " " for x in range(30)]
        rows.append("".join(cells).rstrip())
    joined = " ".join(" ".join(rows).split())
    assert "a reasonably long user message that should be fully visible after wrap" in joined


def test_user_markdown_styling_preserved():
    """Markdown syntax produces styled spans (not literal asterisks)."""
    ctx = RenderContext(width=40)
    lines = UserMarkdown("**bold text** and `code`").render(ctx)
    spans = [s for line in lines for s in line.spans]
    texts = [s.text for s in spans]
    assert "**" not in "".join(texts)  # markdown markers consumed
    # The rendered bold segment carries a bold style
    bold_span = next(s for s in spans if s.text == "bold text")
    assert bold_span.style is not None and bool(bold_span.style.bold)
    # The role prefix carries bold cyan
    prefix_span = spans[0]
    assert prefix_span.text == "> "
    assert prefix_span.style.bold and prefix_span.style.color is not None


def test_user_markdown_cjk_wrap_boundary():
    """CJK wide chars wrap at display-width boundaries — no overflow."""
    text = "你好世界这是一个很长的中文消息需要自动换行处理"
    visible = _render(text, 20)
    for line in visible:
        assert cell_len(line) <= 20
    joined = visible[0][2:] + "".join(line[2:] for line in visible[1:])
    assert joined.replace(" ", "") == text.replace(" ", "")


def test_chat_history_user_routes_to_user_markdown():
    """ChatHistory.add('user', ...) creates a UserMarkdown; others ChatRow."""
    chat = ChatHistory()
    chat.add("user", "hello world")
    chat.add("assistant", "hi")
    chat.add("system", "sys")
    chat.add("tool", "tool")
    assert isinstance(chat.rows[0], UserMarkdown)
    assert isinstance(chat.rows[1], ChatRow)
    assert isinstance(chat.rows[2], ChatRow)
    assert isinstance(chat.rows[3], ChatRow)


def test_chat_history_update_last_updates_user_markdown():
    """update_last() updates the last UserMarkdown row (assistant fallback path)."""
    chat = ChatHistory()
    chat.add("user", "original text")
    chat.update_last("updated text")
    assert isinstance(chat.rows[0], UserMarkdown)
    assert chat.rows[0].text == "updated text"
